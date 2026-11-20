import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from sqlalchemy import URL, delete, select
from strawberry.relay import GlobalID
from test_recording_gate import batch

from phoenix.db import models
from phoenix.db.bulk_inserter import BulkInserter
from phoenix.db.engines import aio_sqlite_engine
from phoenix.server.api.routers.v1.recordings import router as recording_router
from phoenix.server.api.routers.v1.sessions import router as session_router
from phoenix.server.app import _db
from phoenix.server.cost_tracking.span_cost_calculator import SpanCostCalculator
from phoenix.server.daemons.generative_model_store import GenerativeModelStore
from phoenix.server.history_deletion import HistoryDeletionGuard
from phoenix.server.recording import ConversationKey, RecordingGate
from phoenix.server.types import DbSessionFactory
from phoenix.trace.otel import decode_otlp_span


class HistoryDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_purges_receipt_content_but_prevents_replayed_start(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "recording.sqlite3"
            gate = RecordingGate(ledger_path=ledger)
            guard = HistoryDeletionGuard(gate, asyncio.Lock())
            owner, request_id = ConversationKey("codex", str(uuid4())), str(uuid4())
            receipt = gate.start(owner, "private task name", request_id)
            gate.admit(batch((1, owner, None)))
            gate.stop(owner, receipt.capture_id)
            gate.complete_drain(
                receipt.capture_id,
                SimpleNamespace(
                    persisted_span_keys=set(gate.expected_span_keys(receipt.capture_id)),
                    storage="verified",
                    gaps=(),
                    project_relay_id="p",
                    session_relay_id="s",
                ),
            )
            async with guard.deleting():
                guard.mark("task-recordings", receipt.capture_id)
            with self.assertRaises(KeyError):
                gate.status(owner, receipt.capture_id)
            gate.close()
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM span_keys").fetchone()[0], 0)
            restarted = RecordingGate(ledger_path=ledger)
            with self.assertRaisesRegex(ValueError, "permanently deleted"):
                restarted.start(owner, "private task name", request_id)
            restarted.close()

    async def test_live_capture_is_rejected_and_finished_capture_marker_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "recording.sqlite3"
            gate = RecordingGate(ledger_path=ledger)
            guard = HistoryDeletionGuard(gate, asyncio.Lock())
            owner = ConversationKey("codex", str(uuid4()))
            capture = gate.start(owner, "task", str(uuid4()))
            with self.assertRaisesRegex(ValueError, "still recording"):
                guard.check("task-recordings", capture.capture_id)
            gate.stop(owner, capture.capture_id)
            gate.complete_drain(
                capture.capture_id,
                SimpleNamespace(
                    persisted_span_keys=set(),
                    storage="verified",
                    gaps=(),
                    project_relay_id=None,
                    session_relay_id=None,
                ),
            )
            guard.check("task-recordings", capture.capture_id)
            guard.mark("task-recordings", capture.capture_id)
            gate.close()
            restarted = RecordingGate(ledger_path=ledger)
            restored = HistoryDeletionGuard(restarted, asyncio.Lock())
            self.assertTrue(restored.deleted("task-recordings", capture.capture_id))
            self.assertFalse(restored.deleted("other", capture.capture_id))
            restarted.close()

    async def test_active_import_refuses_and_missing_legacy_receipt_allows(self):
        gate = RecordingGate()
        guard = HistoryDeletionGuard(gate, asyncio.Lock())
        guard.check("task-recordings", str(uuid4()))
        with guard.importing():
            with self.assertRaisesRegex(ValueError, "import"):
                guard.check("kiro-session-history", str(uuid4()))
        guard.check("kiro-session-history", str(uuid4()))

    async def test_failed_delete_removes_only_its_new_markers(self):
        gate = RecordingGate()
        guard = HistoryDeletionGuard(gate, asyncio.Lock())
        guard.mark("kiro-session-history", "previous")

        async def confirmed_retained(project, session_id):
            return True

        with self.assertRaisesRegex(RuntimeError, "database failure"):
            async with guard.deleting(confirmed_retained):
                guard.mark("kiro-session-history", "previous")
                guard.mark("kiro-session-history", "failed")
                raise RuntimeError("database failure")
        self.assertTrue(guard.deleted("kiro-session-history", "previous"))
        self.assertFalse(guard.deleted("kiro-session-history", "failed"))

    async def test_cancellation_retains_marker_even_when_primary_read_sees_original_row(self):
        guard = HistoryDeletionGuard(RecordingGate(), asyncio.Lock())

        async def still_visible(project, session_id):
            return True

        with self.assertRaises(asyncio.CancelledError):
            async with guard.deleting(still_visible):
                guard.mark("kiro-session-history", "shielded-commit")
                raise asyncio.CancelledError()
        self.assertTrue(guard.deleted("kiro-session-history", "shielded-commit"))


class HistoryStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.expected = {}
        self.engine = aio_sqlite_engine(
            URL.create(
                "sqlite+aiosqlite", database=str(Path(self.directory.name) / "phoenix.sqlite3")
            ),
            migrate=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(models.Base.metadata.create_all)
        self.db = DbSessionFactory(_db(self.engine), "sqlite")
        self.gate = RecordingGate(ledger_path=Path(self.directory.name) / "receipts.sqlite3")
        self.inserter = BulkInserter(
            self.db,
            event_queue=SimpleQueue(),
            span_cost_calculator=SpanCostCalculator(GenerativeModelStore(self.db)),
        )
        self.guard = HistoryDeletionGuard(self.gate, self.inserter.history_lock)
        self.inserter.history_guard = self.guard
        self.app = FastAPI()
        self.app.state.db = self.db
        self.app.state.recording_gate = self.gate
        self.app.state.history_deletion_guard = self.guard
        self.app.include_router(session_router, prefix="/v1")
        self.app.include_router(recording_router, prefix="/v1")

        @self.app.middleware("http")
        async def state(request, call_next):
            request.state.event_queue = SimpleQueue()
            return await call_next(request)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )

    async def test_reused_row_id_never_deletes_a_new_session(self):
        old = str(uuid4())
        gid = await self.add_session(old, "7")
        async with self.db() as session:
            await session.execute(delete(models.ProjectSession))
        newer = str(uuid4())
        new_gid = await self.add_session(newer, "8")
        self.assertEqual(gid, new_gid, "fixture must actually reuse the numeric row identity")
        response = await self.client.delete(
            "/v1/sessions/" + gid,
            params={
                "expected_session_id": old,
                "expected_project_name": "kiro-session-history",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/v1/sessions/" + new_gid)).status_code, 200)

    async def test_reused_row_id_in_unrelated_project_is_protected(self):
        old = str(uuid4())
        gid = await self.add_session(old, "7")
        async with self.db() as session:
            await session.execute(delete(models.ProjectSession))
        new_gid = await self.add_session(str(uuid4()), "8", project="protected-project")
        self.assertEqual(gid, new_gid)
        response = await self.client.delete(
            "/v1/sessions/" + gid,
            params={
                "expected_session_id": old,
                "expected_project_name": "kiro-session-history",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/v1/sessions/" + new_gid)).status_code, 200)

    async def test_commit_applied_then_error_never_allows_pending_replay(self):
        raw_id = str(uuid4())
        gid = await self.add_session(raw_id, "9")
        otlp = OtlpSpan(
            trace_id=bytes.fromhex("9" * 32),
            span_id=bytes.fromhex("9" * 16),
            name="pending replay",
            start_time_unix_nano=1_000_000_000,
            end_time_unix_nano=2_000_000_000,
        )
        otlp.attributes.add(key="session.id").value.string_value = raw_id
        await self.inserter._enqueue_span(decode_otlp_span(otlp), "kiro-session-history")

        @asynccontextmanager
        async def commit_then_error():
            async with self.db() as session:
                yield session
            raise OSError("COMMIT applied but connection failed")

        self.app.state.db = DbSessionFactory(commit_then_error, "sqlite")
        try:
            with self.assertRaisesRegex(OSError, "COMMIT applied"):
                await self.delete_history(gid)
        finally:
            self.app.state.db = self.db
        await self.inserter._insert_spans(1)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 404)

    async def test_confirmed_primary_rollback_removes_new_marker(self):
        raw_id = str(uuid4())
        gid = await self.add_session(raw_id, "b")
        calls = 0

        @asynccontextmanager
        async def rollback_once():
            nonlocal calls
            calls += 1
            first = calls == 1
            async with self.db() as session:
                yield session
                if first:
                    raise OSError("rolled back before COMMIT")

        self.app.state.db = DbSessionFactory(rollback_once, "sqlite")
        try:
            with self.assertRaisesRegex(OSError, "rolled back"):
                await self.delete_history(gid)
        finally:
            self.app.state.db = self.db
        self.assertFalse(self.guard.deleted("kiro-session-history", raw_id))
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.gate.close()
        await self.engine.dispose()
        self.directory.cleanup()

    async def add_session(self, session_id, trace_byte, project="kiro-session-history"):
        now = datetime.now(timezone.utc)
        async with self.db() as session:
            p = await session.scalar(select(models.Project).where(models.Project.name == project))
            if p is None:
                p = models.Project(name=project, description=None)
                session.add(p)
                await session.flush()
            s = models.ProjectSession(
                session_id=session_id, project_id=p.id, start_time=now, end_time=now
            )
            session.add(s)
            await session.flush()
            trace = models.Trace(
                project_rowid=p.id,
                project_session_rowid=s.id,
                trace_id=trace_byte * 32,
                start_time=now,
                end_time=now,
            )
            session.add(trace)
            await session.flush()
            session.add(
                models.Span(
                    trace_rowid=trace.id,
                    span_id=trace_byte * 16,
                    parent_id=None,
                    name="operation",
                    span_kind="INTERNAL",
                    start_time=now,
                    end_time=now,
                    attributes={"session": {"id": session_id}},
                    events=[],
                    status_code="UNSET",
                    status_message="",
                    cumulative_error_count=0,
                    cumulative_llm_token_count_prompt=0,
                    cumulative_llm_token_count_completion=0,
                )
            )
            session.add(
                models.ProjectSessionAnnotation(
                    project_session_id=s.id,
                    name="note",
                    label="keep scope",
                    metadata_={},
                    annotator_kind="HUMAN",
                    source="API",
                )
            )
            await session.commit()
            gid = str(GlobalID("ProjectSession", str(s.id)))
            self.expected[gid] = {
                "expected_session_id": session_id,
                "expected_project_name": project,
            }
            return gid

    async def delete_history(self, gid):
        return await self.client.delete("/v1/sessions/" + gid, params=self.expected[gid])

    async def test_http_delete_cascades_and_pending_replay_cannot_recreate(self):
        target, keep = str(uuid4()), str(uuid4())
        target_gid = await self.add_session(target, "1")
        keep_gid = await self.add_session(keep, "2")
        capabilities = await self.client.get("/v1/recordings/history/capabilities")
        self.assertEqual(
            capabilities.json(),
            {"version": 1, "permanent_session_delete": True, "stable_identity_delete": True},
        )
        span = OtlpSpan(
            trace_id=bytes.fromhex("1" * 32),
            span_id=bytes.fromhex("1" * 16),
            name="queued replay",
            start_time_unix_nano=1_000_000_000,
            end_time_unix_nano=2_000_000_000,
        )
        span.attributes.add(key="session.id").value.string_value = target
        decoded = decode_otlp_span(span)
        # This span is admitted before the DELETE but written afterwards.
        await self.inserter._enqueue_span(decoded, "kiro-session-history")
        response = await self.delete_history(target_gid)
        self.assertEqual(response.status_code, 204, response.text)
        await self.inserter._insert_spans(1)
        self.assertEqual((await self.client.get("/v1/sessions/" + target_gid)).status_code, 404)
        self.assertEqual((await self.client.get("/v1/sessions/" + keep_gid)).status_code, 200)
        async with self.db.read() as session:
            self.assertEqual(len((await session.scalars(select(models.Trace))).all()), 1)
            self.assertEqual(len((await session.scalars(select(models.Span))).all()), 1)
            self.assertEqual(
                len((await session.scalars(select(models.ProjectSessionAnnotation))).all()), 1
            )
            self.assertEqual(len((await session.scalars(select(models.Project))).all()), 1)

    async def test_http_active_import_refuses_without_changing_storage(self):
        gid = await self.add_session(str(uuid4()), "3")
        with self.guard.importing():
            response = await self.delete_history(gid)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 200)

    async def test_legacy_recording_without_ledger_receipt_deletes_and_replay_stays_blocked(self):
        raw_id = str(uuid4())
        gid = await self.add_session(raw_id, "a", project="task-recordings")
        otlp = OtlpSpan(
            trace_id=bytes.fromhex("a" * 32),
            span_id=bytes.fromhex("a" * 16),
            name="legacy replay",
            start_time_unix_nano=1_000_000_000,
            end_time_unix_nano=2_000_000_000,
        )
        otlp.attributes.add(key="session.id").value.string_value = raw_id
        await self.inserter._enqueue_span(decode_otlp_span(otlp), "task-recordings")
        response = await self.delete_history(gid)
        self.assertEqual(response.status_code, 204, response.text)
        await self.inserter._insert_spans(1)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 404)

    async def test_http_bulk_delete_cannot_bypass_managed_guard(self):
        gid = await self.add_session(str(uuid4()), "4")
        response = await self.client.post(
            "/v1/sessions/delete", json={"session_identifiers": [gid]}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 200)

    async def test_http_server_owned_queue_blocks_import_history_deletion(self):
        gid = await self.add_session(str(uuid4()), "5")
        self.app.state.kiro_session_queue = SimpleNamespace(active=True)
        response = await self.delete_history(gid)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 200)

    async def test_capability_requires_durable_recording_ledger(self):
        self.app.state.history_deletion_guard = HistoryDeletionGuard(
            RecordingGate(), asyncio.Lock()
        )
        response = await self.client.get("/v1/recordings/history/capabilities")
        self.assertFalse(response.json()["permanent_session_delete"])

    async def test_receipt_cleanup_failure_is_explicit_and_restart_recovers(self):
        owner = ConversationKey("codex", str(uuid4()))
        receipt = self.gate.start(owner, "cleanup retry", str(uuid4()))
        self.gate.stop(owner, receipt.capture_id)
        self.gate.complete_drain(
            receipt.capture_id,
            SimpleNamespace(
                persisted_span_keys=set(),
                storage="verified",
                gaps=(),
                project_relay_id=None,
                session_relay_id=None,
            ),
        )
        gid = await self.add_session(receipt.capture_id, "6", project="task-recordings")
        with patch.object(self.gate, "finish_history_deletion", side_effect=OSError("busy ledger")):
            response = await self.delete_history(gid)
        self.assertEqual(response.status_code, 503)
        self.assertIn("receipt cleanup is pending", response.text)
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 404)
        with self.assertRaises(KeyError):
            self.gate.status(owner, receipt.capture_id)
        self.gate.close()
        self.gate = RecordingGate(ledger_path=Path(self.directory.name) / "receipts.sqlite3")
        with self.assertRaises(KeyError):
            self.gate.status(owner, receipt.capture_id)
        self.guard = HistoryDeletionGuard(self.gate, self.inserter.history_lock)
        await self.guard.recover(self.db)
        self.assertEqual(self.gate.pending_history_cleanups(), ())

    async def test_restart_does_not_purge_receipt_when_primary_delete_was_not_committed(self):
        owner = ConversationKey("codex", str(uuid4()))
        receipt = self.gate.start(owner, "pending delete", str(uuid4()))
        self.gate.stop(owner, receipt.capture_id)
        self.gate.complete_drain(
            receipt.capture_id,
            SimpleNamespace(
                persisted_span_keys=set(),
                storage="verified",
                gaps=(),
                project_relay_id=None,
                session_relay_id=None,
            ),
        )
        gid = await self.add_session(receipt.capture_id, "c", project="task-recordings")
        self.guard.mark("task-recordings", receipt.capture_id)
        self.gate.close()
        self.gate = RecordingGate(ledger_path=Path(self.directory.name) / "receipts.sqlite3")
        self.guard = HistoryDeletionGuard(self.gate, self.inserter.history_lock)
        await self.guard.recover(self.db)
        self.assertEqual(
            self.gate.pending_history_cleanups(), (("task-recordings", receipt.capture_id),)
        )
        self.assertEqual((await self.client.get("/v1/sessions/" + gid)).status_code, 200)
