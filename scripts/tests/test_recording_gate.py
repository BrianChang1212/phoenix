import asyncio
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from queue import SimpleQueue
from uuid import uuid4

import httpx
from fastapi import FastAPI
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from sqlalchemy import URL, select

from phoenix.db import models
from phoenix.db.bulk_inserter import BulkInserter
from phoenix.db.engines import aio_sqlite_engine
from phoenix.server.api.routers.v1.recordings import router as recording_router
from phoenix.server.api.routers.v1.traces import post_traces
from phoenix.server.app import _db
from phoenix.server.cost_tracking.span_cost_calculator import SpanCostCalculator
from phoenix.server.daemons.generative_model_store import GenerativeModelStore
from phoenix.server.grpc_server import Servicer
from phoenix.server.recording import ConversationKey, RecordingGate
from phoenix.server.types import DbSessionFactory

OWNER = ConversationKey("codex", "019a74e9-a998-7320-abcd-012345678901")
OTHER = ConversationKey("codex", "019a74e9-a998-7320-abcd-012345678902")
KIRO_OWNER = ConversationKey("kiro", "kiro-session")


def batch(*specs, source="codex"):
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(key="service.name").value.string_value = source
    scope = resource.scope_spans.add()
    for index, owner, parent in specs:
        span = scope.spans.add(
            trace_id=b"t" * 16,
            span_id=index.to_bytes(8, "big"),
            parent_span_id=parent.to_bytes(8, "big") if parent else b"",
            name="operation",
            start_time_unix_nano=time.time_ns(),
            end_time_unix_nano=time.time_ns() + 1,
        )
        if owner:
            span.attributes.add(key="conversation.id").value.string_value = owner.conversation_id
        span.attributes.add(key="thread.id").value.int_value = 42
    return request


def spans(request):
    return [s for r in request.resource_spans for group in r.scope_spans for s in group.spans]


def kiro_batch(session_id, capture_id, index):
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(key="openinference.project.name").value.string_value = "kiro-local"
    scope = resource.scope_spans.add()
    span = scope.spans.add(
        trace_id=b"k" * 16,
        span_id=index.to_bytes(8, "big"),
        name="execute_pwsh",
        start_time_unix_nano=time.time_ns(),
        end_time_unix_nano=time.time_ns() + 1,
    )
    span.attributes.add(key="session.id").value.string_value = session_id
    span.attributes.add(key="recording.capture_id").value.string_value = capture_id
    return request


class RecordingGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = RecordingGate()

    def start(self, owner=OWNER, task="test"):
        return self.gate.start(owner, task, str(uuid4()))

    def test_off_drops_managed_but_preserves_unrelated(self):
        self.assertEqual(spans(self.gate.admit(batch((1, OWNER, 0)))), [])
        incoming = batch((1, None, 0), source="unrelated")
        self.assertEqual(self.gate.admit(incoming), incoming)

    def test_only_owner_and_verified_ancestors_are_admitted(self):
        capture = self.start()
        admitted = spans(
            self.gate.admit(
                batch(
                    (2, None, 1),
                    (1, OWNER, 0),
                    (3, OTHER, 0),
                    (4, None, 0),
                )
            )
        )
        self.assertEqual(len(admitted), 2)
        for span in admitted:
            attrs = {a.key: a.value.string_value for a in span.attributes}
            self.assertEqual(attrs["session.id"], capture.capture_id)
            self.assertEqual(attrs["recording.original_trace_id"], (b"t" * 16).hex())
        self.assertNotEqual(admitted[0].trace_id, b"t" * 16)

    def test_conflicting_duplicate_evidence_rejects_descendants(self):
        self.start()
        self.start(OTHER)
        self.assertEqual(
            spans(
                self.gate.admit(
                    batch(
                        (1, OWNER, 0),
                        (1, OTHER, 0),
                        (2, None, 1),
                    )
                )
            ),
            [],
        )

    def test_direct_owner_conflicting_with_ancestor_is_rejected(self):
        self.start()
        admitted = spans(self.gate.admit(batch((1, OTHER, 0), (2, OWNER, 1))))
        self.assertEqual(admitted, [])

    def test_stop_and_new_capture_never_reuse_session_or_old_work(self):
        first = self.start()
        incoming = batch((1, OWNER, 0))
        first_span = spans(self.gate.admit(incoming))[0]
        stopped = self.gate.stop(OWNER, first.capture_id)
        self.assertEqual(stopped.state, "incomplete")
        self.assertEqual(stopped.storage, "unverified")
        self.assertEqual(spans(self.gate.admit(batch((2, OWNER, 0)))), [])
        second = self.start()
        self.assertNotEqual(first.capture_id, second.capture_id)
        self.assertEqual(spans(self.gate.admit(incoming)), [])
        second_span = spans(self.gate.admit(batch((3, OWNER, 0))))[0]
        self.assertNotEqual(first_span.trace_id, second_span.trace_id)

    def test_commands_are_idempotent_and_owner_scoped(self):
        request_id = str(uuid4())
        first = self.gate.start(OWNER, "test", request_id)
        self.assertEqual(first, self.gate.start(OWNER, "test", request_id))
        with self.assertRaises(ValueError):
            self.gate.start(OWNER, "different", str(uuid4()))
        with self.assertRaises(KeyError):
            self.gate.stop(OTHER, first.capture_id)
        stopped = self.gate.stop(OWNER, first.capture_id)
        self.assertEqual(stopped.state, "empty")
        self.assertEqual(stopped, self.gate.stop(OWNER, first.capture_id))
        with self.assertRaises(ValueError):
            self.gate.start(OWNER, "different", request_id)

    def test_batch_limit_fails_closed_for_managed_spans(self):
        gate = RecordingGate(max_batch_spans=1)
        gate.start(OWNER, "test", str(uuid4()))
        self.assertEqual(spans(gate.admit(batch((1, OWNER, 0), (2, OWNER, 0)))), [])
        self.assertEqual(gate.receiver_gaps["codex:batch_limit"], 2)

    def test_unknown_spans_do_not_assign_gaps_to_other_conversations(self):
        self.start()
        self.start(OTHER)
        self.gate.admit(batch((1, None, 0)))
        self.assertEqual(self.gate.status(OWNER).gaps, ())
        self.assertEqual(self.gate.status(OTHER).gaps, ())
        self.assertEqual(self.gate.receiver_gaps["codex:unknown_or_conflicting_owner"], 1)

    def test_cross_start_boundary_preserves_original_times_and_marks_overlap(self):
        capture = self.start()
        incoming = batch((1, OWNER, 0), (2, OWNER, 0))
        crossing, old = spans(incoming)
        crossing.start_time_unix_nano = capture.started_at_ns - 100
        crossing.end_time_unix_nano = capture.started_at_ns + 100
        old.start_time_unix_nano = capture.started_at_ns - 200
        old.end_time_unix_nano = capture.started_at_ns - 100
        admitted = spans(self.gate.admit(incoming))
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0].start_time_unix_nano, crossing.start_time_unix_nano)
        attrs = {a.key: a.value.string_value for a in admitted[0].attributes}
        self.assertEqual(attrs["recording.boundary"], "crosses_start")

    def test_worker_integer_is_not_a_conversation_owner(self):
        self.start()
        self.assertEqual(spans(self.gate.admit(batch((1, None, 0)))), [])

    def test_kiro_events_must_match_the_active_capture_id(self):
        first = self.gate.start(KIRO_OWNER, "first", str(uuid4()))
        admitted = spans(self.gate.admit(kiro_batch(KIRO_OWNER.conversation_id, first.capture_id, 1)))
        self.assertEqual(len(admitted), 1)
        self.gate.stop(KIRO_OWNER, first.capture_id)
        second = self.gate.start(KIRO_OWNER, "second", str(uuid4()))
        self.assertEqual(spans(self.gate.admit(kiro_batch(KIRO_OWNER.conversation_id, first.capture_id, 2))), [])
        self.assertEqual(self.gate.status(KIRO_OWNER, second.capture_id).queued_spans, 0)
        self.assertEqual(len(spans(self.gate.admit(kiro_batch(KIRO_OWNER.conversation_id, second.capture_id, 3)))), 1)
        self.assertEqual(self.gate.receiver_gaps["kiro:capture_mismatch"], 1)


class RecordingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.state.recording_gate = RecordingGate()
        self.app.include_router(recording_router, prefix="/v1")
        self.app.add_api_route("/v1/traces", post_traces, methods=["POST"])
        self.stored = []

        async def enqueue(span, project):
            self.stored.append((span, project))

        self.enqueue_span = enqueue

        @self.app.middleware("http")
        async def enqueue_state(request, call_next):
            request.state.enqueue_span = self.enqueue_span
            return await call_next(request)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def send(self, request):
        return await self.client.post(
            "/v1/traces",
            content=request.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        )

    async def test_control_to_ingest_to_stop(self):
        self.assertEqual((await self.send(batch((1, OWNER, 0)))).status_code, 200)
        self.assertEqual(self.stored, [])
        command = dict(asdict(OWNER), task="Verify", request_id=str(uuid4()))
        response = await self.client.post("/v1/recordings/start", json=command)
        self.assertEqual(response.status_code, 200, response.text)
        capture = response.json()
        self.assertEqual(capture["state"], "recording")
        self.assertEqual((await self.send(batch((2, OWNER, 0), (3, OTHER, 0)))).status_code, 200)
        self.assertEqual(len(self.stored), 1)
        self.assertEqual(self.stored[0][1], "task-recordings")
        stop = dict(asdict(OWNER), capture_id=capture["capture_id"])
        response = await self.client.post("/v1/recordings/stop", json=stop)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "incomplete")
        await self.send(batch((4, OWNER, 0)))
        self.assertEqual(len(self.stored), 1)
        unrelated = batch((5, None, 0), source="unrelated")
        spans(unrelated)[0].attributes.add(key="recording").value.string_value = "external-value"
        await self.send(unrelated)
        self.assertEqual(self.stored[-1][1], "default")

    async def test_disabled_and_foreign_controls_are_rejected(self):
        del self.app.state.recording_gate
        response = await self.client.post("/v1/recordings/status", json=asdict(OWNER))
        self.assertEqual(response.status_code, 503)
        self.app.state.recording_gate = RecordingGate()
        receipt = self.app.state.recording_gate.start(OWNER, "test", str(uuid4()))
        response = await self.client.post(
            "/v1/recordings/stop",
            json=dict(
                asdict(OTHER),
                capture_id=receipt.capture_id,
            ),
        )
        self.assertEqual(response.status_code, 404)

    async def test_remote_control_is_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("192.0.2.1", 1234)),
            base_url="http://127.0.0.1",
        ) as remote:
            response = await remote.post("/v1/recordings/status", json=asdict(OWNER))
        self.assertEqual(response.status_code, 403)

    async def test_grpc_cannot_bypass_managed_gate(self):
        gate = self.app.state.recording_gate
        gate.start(OWNER, "test", str(uuid4()))
        servicer = Servicer(self.enqueue_span, recording_gate=gate)
        response = await servicer.Export(batch((1, OWNER, 0)), None)
        self.assertEqual(response.partial_success.rejected_spans, 1)
        self.assertEqual(self.stored, [])
        await servicer.Export(batch((2, None, 0), source="unrelated"), None)
        self.assertEqual(len(self.stored), 1)

    async def test_managed_ingress_has_a_bounded_request_body(self):
        self.app.state.recording_gate.max_request_bytes = 32
        response = await self.send(batch((1, OWNER, 0)))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.stored, [])

    async def test_actual_sqlite_persistence_is_isolated_by_capture(self):
        evidence = Path(".audit")
        evidence.mkdir(exist_ok=True)
        database_path = evidence / f"recording-{uuid4()}.db"
        engine = aio_sqlite_engine(
            URL.create("sqlite+aiosqlite", database=str(database_path)), migrate=False
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(models.Base.metadata.create_all)
            db = DbSessionFactory(_db(engine), "sqlite")
            inserter = BulkInserter(
                db,
                event_queue=SimpleQueue(),
                span_cost_calculator=SpanCostCalculator(GenerativeModelStore(db)),
            )
            async with inserter as (_, enqueue_span, _):
                self.enqueue_span = enqueue_span

                await self.send(batch((10, OWNER, 0)))
                async with db() as session:
                    self.assertEqual(list(await session.scalars(select(models.Span))), [])
                started = await self.client.post(
                    "/v1/recordings/start",
                    json=dict(
                        asdict(OWNER),
                        task="SQLite verification",
                        request_id=str(uuid4()),
                    ),
                )
                capture = started.json()["capture_id"]
                await self.send(batch((11, OWNER, 0), (12, OTHER, 0), (13, None, 0)))
                for _ in range(100):
                    async with db() as session:
                        rows = list(await session.scalars(select(models.Span)))
                    if rows:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].span_id, (11).to_bytes(8, "big").hex())
                self.assertEqual(rows[0].attributes["session"]["id"], capture)
                async with db() as session:
                    self.assertEqual(
                        list(await session.scalars(select(models.Project.name))),
                        ["task-recordings"],
                    )
                    self.assertEqual(
                        list(await session.scalars(select(models.ProjectSession.session_id))),
                        [capture],
                    )
                await self.client.post(
                    "/v1/recordings/stop",
                    json=dict(
                        asdict(OWNER),
                        capture_id=capture,
                    ),
                )
                await self.send(batch((14, OWNER, 0)))
            async with db() as session:
                rows = list(await session.scalars(select(models.Span)))
                self.assertEqual(len(rows), 1)
            print(f"SQLite recording evidence: {database_path.resolve()}")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
