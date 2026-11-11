import asyncio
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch
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
from phoenix.server.recording_readback import read_recording_spans
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


def kiro_batch(
    session_id,
    capture_id,
    index,
    event_id=None,
    source_event_ids=None,
    result=None,
    record_kind=None,
):
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
    if event_id:
        span.attributes.add(key="kiro.event_id").value.string_value = event_id
    if source_event_ids:
        span.attributes.add(key="kiro.source_event_ids").value.string_value = ",".join(source_event_ids)
    if result:
        span.attributes.add(key="kiro.result").value.string_value = result
    if record_kind:
        span.attributes.add(key="kiro.record_kind").value.string_value = record_kind
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
        self.assertEqual(stopped.state, "draining")
        self.assertEqual(stopped.storage, "unverified")
        self.assertEqual(spans(self.gate.admit(batch((2, OWNER, 0)))), [])
        self.gate.tick(now_ns=stopped.drain_deadline_ns)
        second = self.start()
        self.assertNotEqual(first.capture_id, second.capture_id)
        self.assertEqual(spans(self.gate.admit(incoming)), [])
        second_span = spans(self.gate.admit(batch((3, OWNER, 0))))[0]
        self.assertNotEqual(first_span.trace_id, second_span.trace_id)

    def test_idle_lease_expires_into_a_bounded_drain(self):
        now = [1_000_000_000]
        gate = RecordingGate(
            clock_ns=lambda: now[0], idle_lease_sec=5, drain_grace_sec=2
        )
        capture = gate.start(OWNER, "lease", str(uuid4()))
        gate.admit(batch((1, OWNER, 0)))
        now[0] = capture.idle_lease_deadline_ns
        gate.tick(now_ns=now[0])
        self.assertEqual(gate.status(OWNER, capture.capture_id).state, "draining")
        self.assertEqual(gate.draining_captures_due(now_ns=now[0]), ())
        now[0] += 2_000_000_000
        gate.tick(now_ns=now[0])
        due = gate.draining_captures_due(now_ns=now[0])
        self.assertEqual([item.capture_id for item in due], [capture.capture_id])

    def test_hard_lease_expires_even_when_idle_deadline_is_later(self):
        now = [1_000_000_000]
        gate = RecordingGate(
            clock_ns=lambda: now[0], idle_lease_sec=100, hard_limit_sec=5, drain_grace_sec=2
        )
        capture = gate.start(OWNER, "hard limit", str(uuid4()))
        now[0] = capture.hard_deadline_ns
        gate.tick(now_ns=now[0])
        self.assertEqual(gate.status(OWNER, capture.capture_id).state, "draining")
        self.assertIn("hard_limit_expired", gate.status(OWNER, capture.capture_id).gaps)

    def test_truncated_kiro_manifest_keeps_readback_incomplete(self):
        gate = RecordingGate()
        capture = gate.start(KIRO_OWNER, "truncated manifest", str(uuid4()))
        stopping = gate.stop(
            KIRO_OWNER,
            capture.capture_id,
            observed_event_ids=["event-a"],
            manifest_complete=False,
        )
        gate.admit(
            kiro_batch(KIRO_OWNER.conversation_id, capture.capture_id, 1, event_id="event-a")
        )
        gate.tick(now_ns=stopping.drain_deadline_ns)
        due = gate.draining_captures_due(now_ns=stopping.drain_deadline_ns)
        result = SimpleNamespace(
            persisted_span_keys=frozenset(gate.expected_span_keys(capture.capture_id)),
            storage="verified",
            project_relay_id="project",
            session_relay_id="session",
        )
        final = gate.complete_drain(due[0].capture_id, result)
        self.assertEqual(final.state, "incomplete")
        self.assertFalse(final.manifest_complete)
        self.assertIn("kiro_manifest_incomplete", final.gaps)

    def test_exact_primary_readback_is_required_before_timeline_url(self):
        gate = RecordingGate(timeline_origin="https://localhost:7443")
        capture = gate.start(OWNER, "readback", str(uuid4()))
        gate.admit(batch((1, OWNER, 0)))
        stopping = gate.stop(OWNER, capture.capture_id)
        gate.tick(now_ns=stopping.drain_deadline_ns)
        keys = frozenset(gate.expected_span_keys(capture.capture_id))
        result = SimpleNamespace(
            persisted_span_keys=keys,
            storage="verified",
            project_relay_id="UHJvamVjdDox",
            session_relay_id="UHJvamVjdFNlc3Npb246Mg==",
            gaps=(),
        )
        final = gate.complete_drain(capture.capture_id, result)
        self.assertEqual(final.state, "observed")
        self.assertEqual(
            final.timeline_url,
            "https://localhost:7443/projects/UHJvamVjdDox/sessions/"
            "UHJvamVjdFNlc3Npb246Mg%3D%3D?sessionView=timeline",
        )

    def test_failed_primary_readback_is_one_shot_and_has_no_timeline_url(self):
        gate = RecordingGate()
        capture = gate.start(OWNER, "readback failure", str(uuid4()))
        gate.admit(batch((1, OWNER, 0)))
        stopping = gate.stop(OWNER, capture.capture_id)
        gate.tick(now_ns=stopping.drain_deadline_ns)
        self.assertTrue(gate.begin_readback(capture.capture_id))
        failed = gate.fail_readback(capture.capture_id)
        self.assertFalse(gate.begin_readback(capture.capture_id))
        self.assertEqual(failed.state, "incomplete")
        self.assertIsNone(failed.timeline_url)
        self.assertIn("storage_readback_failed", failed.gaps)
        self.assertEqual(
            gate.draining_captures_due(now_ns=stopping.drain_deadline_ns), ()
        )

    def test_timeline_origin_must_be_loopback(self):
        with self.assertRaises(ValueError):
            RecordingGate(timeline_origin="https://phoenix.example.com")

    def test_stop_drains_only_previously_observed_span_keys(self):
        capture = self.start()
        original = batch((1, OWNER, 0))
        mapped = spans(self.gate.admit(original))[0]

        stopping = self.gate.stop(OWNER, capture.capture_id)
        self.assertEqual(stopping.state, "draining")
        self.assertEqual(
            [span.span_id for span in spans(self.gate.admit(batch((1, OWNER, 0), (2, OWNER, 0))))],
            [mapped.span_id],
        )
        with self.assertRaises(ValueError):
            self.start()

        self.gate.tick(now_ns=stopping.drain_deadline_ns)
        finalized = self.gate.status(OWNER, capture.capture_id)
        self.assertEqual(finalized.state, "incomplete")
        self.assertIn("storage_unverified", finalized.gaps)
        self.assertIn("late_unobserved_operation", finalized.gaps)
        self.assertEqual(spans(self.gate.admit(batch((3, OWNER, 0)))), [])

    def test_commands_are_idempotent_and_owner_scoped(self):
        request_id = str(uuid4())
        first = self.gate.start(OWNER, "test", request_id)
        self.assertEqual(first, self.gate.start(OWNER, "test", request_id))
        with self.assertRaises(ValueError):
            self.gate.start(OWNER, "different", str(uuid4()))
        with self.assertRaises(KeyError):
            self.gate.stop(OTHER, first.capture_id)
        stopped = self.gate.stop(OWNER, first.capture_id)
        self.assertEqual(stopped.state, "draining")
        stopped = self.gate.tick(now_ns=stopped.drain_deadline_ns)[0]
        self.assertEqual(stopped.state, "empty")
        self.assertEqual(stopped, self.gate.stop(OWNER, first.capture_id))
        with self.assertRaises(ValueError):
            self.gate.start(OWNER, "different", request_id)

    def test_restart_marks_active_capture_incomplete_and_keeps_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "recordings.sqlite3"
            request_id = str(uuid4())
            first = RecordingGate(ledger_path=ledger_path)
            started = first.start(OWNER, "task", request_id)
            first.close()

            recovered = RecordingGate(ledger_path=ledger_path)
            try:
                receipt = recovered.status(OWNER, started.capture_id)
                self.assertEqual(receipt.state, "incomplete")
                self.assertIn("receiver_restart", receipt.gaps)
                self.assertEqual(recovered.start(OWNER, "task", request_id), receipt)
            finally:
                recovered.close()

    def test_database_binding_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "recordings.sqlite3"
            gate = RecordingGate(ledger_path=ledger_path, database_binding="db-a")
            gate.close()
            with self.assertRaisesRegex(RuntimeError, "different Phoenix database"):
                RecordingGate(ledger_path=ledger_path, database_binding="db-b")

    def test_start_ledger_failure_does_not_leave_an_active_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = RecordingGate(ledger_path=Path(directory) / "recordings.sqlite3")
            request_id = str(uuid4())
            try:
                with patch.object(gate._ledger, "save_capture", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        gate.start(OWNER, "persist receipt", request_id)

                self.assertIsNone(gate.status(OWNER))
                self.assertEqual(spans(gate.admit(batch((1, OWNER, 0)))), [])
                retry = gate.start(OWNER, "persist receipt", request_id)
                self.assertEqual(retry.state, "recording")
            finally:
                gate.close()

    def test_recording_ledger_process_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "recordings.sqlite3"
            owner = RecordingGate(ledger_path=ledger_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    RecordingGate(ledger_path=ledger_path)
            finally:
                owner.close()

    def test_restart_reads_back_persisted_spans_without_resuming_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "recordings.sqlite3"
            first = RecordingGate(ledger_path=ledger_path)
            capture = first.start(OWNER, "restart readback", str(uuid4()))
            first.admit(batch((1, OWNER, 0), (2, OWNER, 0)))
            first.close()

            recovered = RecordingGate(ledger_path=ledger_path)
            try:
                restarted = recovered.status(OWNER, capture.capture_id)
                self.assertEqual(restarted.state, "incomplete")
                self.assertIn("receiver_restart", restarted.gaps)
                self.assertEqual(spans(recovered.admit(batch((3, OWNER, 0)))), [])
                due = recovered.draining_captures_due(now_ns=time.time_ns())
                self.assertEqual([item.capture_id for item in due], [capture.capture_id])
                self.assertTrue(recovered.begin_readback(capture.capture_id))
                expected = frozenset(recovered.expected_span_keys(capture.capture_id))
                result = SimpleNamespace(
                    persisted_span_keys=frozenset({next(iter(expected))}),
                    storage="partial",
                    project_relay_id="project",
                    session_relay_id="session",
                )
                verified = recovered.complete_drain(capture.capture_id, result)
                self.assertEqual(verified.state, "incomplete")
                self.assertEqual(verified.storage, "partial")
                self.assertIn("receiver_restart", verified.gaps)
                self.assertIsNotNone(verified.timeline_url)
                self.assertFalse(recovered.begin_readback(capture.capture_id))
            finally:
                recovered.close()

    def test_capture_span_limit_drains_partial_data_for_readback(self):
        gate = RecordingGate(max_capture_spans=1, drain_grace_sec=1)
        capture = gate.start(OWNER, "capture limit", str(uuid4()))
        self.assertEqual(len(spans(gate.admit(batch((1, OWNER, 0))))), 1)
        self.assertEqual(spans(gate.admit(batch((2, OWNER, 0)))), [])
        limited = gate.status(OWNER, capture.capture_id)
        self.assertEqual(limited.state, "draining")
        self.assertIn("capture_limit", limited.gaps)
        due_at = limited.drain_deadline_ns
        gate.tick(now_ns=due_at)
        due = gate.draining_captures_due(now_ns=due_at)
        self.assertEqual(len(due), 1)
        self.assertTrue(gate.begin_readback(capture.capture_id))
        result = SimpleNamespace(
            persisted_span_keys=frozenset(gate.expected_span_keys(capture.capture_id)),
            storage="verified",
            project_relay_id="project",
            session_relay_id="session",
        )
        final = gate.complete_drain(capture.capture_id, result)
        self.assertEqual(final.state, "incomplete")
        self.assertEqual(final.storage, "verified")
        self.assertIn("capture_limit", final.gaps)
        self.assertIsNotNone(final.timeline_url)

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
        stopped = self.gate.stop(KIRO_OWNER, first.capture_id)
        self.gate.tick(now_ns=stopped.drain_deadline_ns)
        second = self.gate.start(KIRO_OWNER, "second", str(uuid4()))
        self.assertEqual(spans(self.gate.admit(kiro_batch(KIRO_OWNER.conversation_id, first.capture_id, 2))), [])
        self.assertEqual(self.gate.status(KIRO_OWNER, second.capture_id).queued_spans, 0)
        self.assertEqual(len(spans(self.gate.admit(kiro_batch(KIRO_OWNER.conversation_id, second.capture_id, 3)))), 1)
        self.assertEqual(self.gate.receiver_gaps["kiro:capture_mismatch"], 1)

    def test_kiro_drain_accepts_only_event_ids_in_the_frozen_manifest(self):
        capture = self.gate.start(KIRO_OWNER, "task", str(uuid4()))
        stopping = self.gate.stop(
            KIRO_OWNER, capture.capture_id, observed_event_ids=("event-a",)
        )

        admitted = spans(
            self.gate.admit(
                kiro_batch(KIRO_OWNER.conversation_id, capture.capture_id, 1, "event-a")
            )
        )
        rejected = spans(
            self.gate.admit(
                kiro_batch(KIRO_OWNER.conversation_id, capture.capture_id, 2, "event-b")
            )
        )

        self.assertEqual(len(admitted), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(
            self.gate.status(KIRO_OWNER, capture.capture_id).state, "draining"
        )
        self.assertEqual(self.gate.status(KIRO_OWNER, capture.capture_id).gaps, ("late_unobserved_operation",))
        self.gate.tick(now_ns=stopping.drain_deadline_ns)
        self.assertEqual(
            self.gate.status(KIRO_OWNER, capture.capture_id).state, "incomplete"
        )

    def test_unmatched_kiro_hook_observation_is_incomplete_after_verified_readback(self):
        capture = self.gate.start(KIRO_OWNER, "partial hook", str(uuid4()))
        self.gate.admit(
            kiro_batch(
                KIRO_OWNER.conversation_id,
                capture.capture_id,
                1,
                event_id="partial-event",
                result="incomplete",
            )
        )
        stopping = self.gate.stop(
            KIRO_OWNER, capture.capture_id, ["partial-event"], manifest_complete=True
        )
        self.gate.tick(now_ns=stopping.drain_deadline_ns)
        due = self.gate.draining_captures_due(now_ns=stopping.drain_deadline_ns)
        keys = frozenset(self.gate.expected_span_keys(capture.capture_id))
        result = SimpleNamespace(
            persisted_span_keys=keys,
            storage="verified",
            project_relay_id="project",
            session_relay_id="session",
        )
        final = self.gate.complete_drain(due[0].capture_id, result)
        self.assertEqual(final.state, "incomplete")
        self.assertIn("kiro_incomplete_operation", final.gaps)

    def test_ambiguous_and_point_kiro_observations_cannot_be_verified(self):
        for index, attributes, expected_gap in (
            (1, {"result": "ambiguous"}, "kiro_unpaired_observation"),
            (
                2,
                {"record_kind": "event observation; no operation duration"},
                "kiro_unpaired_observation",
            ),
        ):
            with self.subTest(attributes=attributes):
                gate = RecordingGate()
                capture = gate.start(KIRO_OWNER, "ambiguous hook", str(uuid4()))
                gate.admit(
                    kiro_batch(
                        KIRO_OWNER.conversation_id,
                        capture.capture_id,
                        index,
                        event_id=f"event-{index}",
                        **attributes,
                    )
                )
                stopping = gate.stop(
                    KIRO_OWNER,
                    capture.capture_id,
                    [f"event-{index}"],
                    manifest_complete=True,
                )
                gate.tick(now_ns=stopping.drain_deadline_ns)
                due = gate.draining_captures_due(now_ns=stopping.drain_deadline_ns)
                result = SimpleNamespace(
                    persisted_span_keys=frozenset(gate.expected_span_keys(capture.capture_id)),
                    storage="verified",
                    project_relay_id="project",
                    session_relay_id="session",
                )
                final = gate.complete_drain(due[0].capture_id, result)
                self.assertEqual(final.state, "incomplete")
                self.assertIn(expected_gap, final.gaps)


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
        self.assertEqual(response.json()["state"], "draining")
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
                stopped = await self.client.post(
                    "/v1/recordings/stop",
                    json=dict(
                        asdict(OWNER),
                        capture_id=capture,
                    ),
                )
                self.assertEqual(stopped.status_code, 200, stopped.text)
                gate = self.app.state.recording_gate
                deadline = stopped.json()["drain_deadline_ns"]
                gate.tick(now_ns=deadline)
                self.assertTrue(gate.begin_readback(capture))
                result = await read_recording_spans(
                    db,
                    capture_id=capture,
                    expected_span_keys=set(gate.expected_span_keys(capture)),
                )
                final = gate.complete_drain(capture, result)
                self.assertEqual(final.state, "observed")
                self.assertEqual(final.storage, "verified")
                self.assertEqual(final.persisted_spans, 1)
                self.assertIsNotNone(final.timeline_url)

                status = await self.client.post(
                    "/v1/recordings/status",
                    json=dict(asdict(OWNER), capture_id=capture),
                )
                self.assertEqual(status.status_code, 200, status.text)
                self.assertEqual(status.json()["state"], "observed")
                await self.send(batch((14, OWNER, 0)))
            async with db() as session:
                rows = list(await session.scalars(select(models.Span)))
                self.assertEqual(len(rows), 1)
            print(f"SQLite recording evidence: {database_path.resolve()}")
        finally:
            await engine.dispose()

    async def test_codex_and_kiro_interleaved_recordings_read_back_independently(self):
        evidence = Path(".audit")
        evidence.mkdir(exist_ok=True)
        database_path = evidence / f"recording-mixed-{uuid4()}.db"
        engine = aio_sqlite_engine(
            URL.create("sqlite+aiosqlite", database=str(database_path)), migrate=False
        )
        gate = RecordingGate(drain_grace_sec=0.01)
        self.app.state.recording_gate = gate
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
                codex_start = await self.client.post(
                    "/v1/recordings/start",
                    json=dict(
                        asdict(OWNER), task="Codex mixed test", request_id=str(uuid4())
                    ),
                )
                kiro_start = await self.client.post(
                    "/v1/recordings/start",
                    json=dict(
                        asdict(KIRO_OWNER), task="Kiro mixed test", request_id=str(uuid4())
                    ),
                )
                self.assertEqual(codex_start.status_code, 200, codex_start.text)
                self.assertEqual(kiro_start.status_code, 200, kiro_start.text)
                codex_capture = codex_start.json()["capture_id"]
                kiro_capture = kiro_start.json()["capture_id"]

                await self.send(batch((20, OWNER, 0), (21, OTHER, 0)))
                first_event = str(uuid4())
                delayed_event = str(uuid4())
                rejected_event = str(uuid4())
                await self.send(
                    kiro_batch(
                        KIRO_OWNER.conversation_id,
                        kiro_capture,
                        22,
                        event_id=first_event,
                    )
                )

                codex_stop = await self.client.post(
                    "/v1/recordings/stop",
                    json=dict(asdict(OWNER), capture_id=codex_capture),
                )
                kiro_stop = await self.client.post(
                    "/v1/recordings/stop",
                    json=dict(
                        asdict(KIRO_OWNER),
                        capture_id=kiro_capture,
                        observed_event_ids=[first_event, delayed_event],
                        manifest_complete=True,
                    ),
                )
                self.assertEqual(codex_stop.status_code, 200, codex_stop.text)
                self.assertEqual(kiro_stop.status_code, 200, kiro_stop.text)

                await self.send(batch((23, OWNER, 0)))
                await self.send(
                    kiro_batch(
                        KIRO_OWNER.conversation_id,
                        kiro_capture,
                        24,
                        event_id=delayed_event,
                    )
                )
                await self.send(
                    kiro_batch(
                        KIRO_OWNER.conversation_id,
                        kiro_capture,
                        25,
                        event_id=rejected_event,
                    )
                )

                for _ in range(100):
                    async with db() as session:
                        rows = list(await session.scalars(select(models.Span)))
                    if len(rows) >= 3:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(rows), 3)

                deadline = max(
                    codex_stop.json()["drain_deadline_ns"],
                    kiro_stop.json()["drain_deadline_ns"],
                )
                gate.tick(now_ns=deadline)
                for capture_id in (codex_capture, kiro_capture):
                    due = gate.draining_captures_due(now_ns=deadline)
                    self.assertIn(capture_id, {item.capture_id for item in due})
                    result = await read_recording_spans(
                        db,
                        capture_id=capture_id,
                        expected_span_keys=set(gate.expected_span_keys(capture_id)),
                    )
                    final = gate.complete_drain(capture_id, result)
                    if capture_id == codex_capture:
                        self.assertEqual(final.state, "incomplete")
                        self.assertEqual(final.storage, "verified")
                        self.assertEqual(final.persisted_spans, 1)
                        self.assertIn("late_unobserved_operation", final.gaps)
                        self.assertIsNotNone(final.timeline_url)
                    else:
                        self.assertEqual(final.state, "incomplete")
                        self.assertEqual(final.storage, "verified")
                        self.assertEqual(final.persisted_spans, 2)
                        self.assertIn("late_unobserved_operation", final.gaps)
                        self.assertIsNotNone(final.timeline_url)

                async with db() as session:
                    persisted = list(await session.scalars(select(models.Span)))
                capture_ids = [
                    row.attributes["recording"]["capture_id"] for row in persisted
                ]
                self.assertEqual(capture_ids.count(codex_capture), 1)
                self.assertEqual(capture_ids.count(kiro_capture), 2)

                codex_status = await self.client.post(
                    "/v1/recordings/status",
                    json=dict(asdict(OWNER), capture_id=codex_capture),
                )
                kiro_status = await self.client.post(
                    "/v1/recordings/status",
                    json=dict(asdict(KIRO_OWNER), capture_id=kiro_capture),
                )
                self.assertEqual(codex_status.json()["state"], "incomplete")
                self.assertEqual(kiro_status.json()["state"], "incomplete")
                self.assertEqual(kiro_status.json()["persisted_spans"], 2)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
