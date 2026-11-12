import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import psutil
from fastapi import FastAPI
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from phoenix.server.api.routers.v1.recordings import router
from phoenix.server.recording import ConversationKey, RecordingGate
from phoenix.server.recording_lifetime import (
    LifetimeBinding,
    LifetimeProbe,
    maintain_lifetimes,
    validate_local_binding,
)

SESSION = "sess_f768a35d-90a5-45e5-a6eb-fcb2886ae323"
OWNER = ConversationKey("kiro", SESSION)


class LifetimeFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.session = self.root / ".kiro/sessions/0123456789abcdef" / SESSION
        self.session.mkdir(parents=True)
        (self.session / "session.json").write_text(json.dumps({"id": SESSION}))
        self.transcript = self.session / "messages.jsonl"
        self.transcript.touch()
        self.append("turn_start", "a")

    def append(self, kind, ident, **extra):
        with self.transcript.open("ab") as stream:
            stream.write(
                (
                    json.dumps(
                        {
                            "payload": {
                                "type": kind,
                                "turnId": ident,
                                "boundarySource": "observed",
                                **extra,
                            }
                        }
                    )
                    + "\n"
                ).encode()
            )

    def binding(self, process=None):
        owner = psutil.Process(process.pid if process else None)
        stat = self.transcript.stat()
        return LifetimeBinding.model_validate(
            {
                "owner": {"pid": owner.pid, "created": owner.create_time(), "exe": owner.exe()},
                "transcript": str(self.transcript),
                "file_id": [stat.st_dev, stat.st_ino],
                "offset": stat.st_size,
                "turn_id": "a",
            }
        )


class LifetimeTests(LifetimeFixture, unittest.TestCase):
    def test_capture_limit_releases_lifetime_observation(self):
        gate = RecordingGate(max_capture_spans=1)
        receipt = gate.start(OWNER, "limit", str(uuid4()), lifetime=self.binding())
        request = ExportTraceServiceRequest()
        resource = request.resource_spans.add()
        resource.resource.attributes.add(key="service.name").value.string_value = "kiro"
        scope = resource.scope_spans.add()
        for index in (1, 2):
            span = scope.spans.add(
                trace_id=b"k" * 16,
                span_id=index.to_bytes(8, "big"),
                name="operation",
                start_time_unix_nano=time.time_ns(),
                end_time_unix_nano=time.time_ns() + 1,
            )
            span.attributes.add(key="session.id").value.string_value = SESSION
            span.attributes.add(key="recording.capture_id").value.string_value = receipt.capture_id
        gate.admit(request)
        self.assertEqual(gate.status(OWNER, receipt.capture_id).state, "draining")
        self.assertEqual(gate.active_lifetimes(), ())

    def test_closing_gate_releases_all_lifetime_observations(self):
        gate = RecordingGate()
        gate.start(OWNER, "close", str(uuid4()), lifetime=self.binding())
        gate.close()
        self.assertEqual(gate.active_lifetimes(), ())

    def test_process_exit_closes_exact_capture_with_incomplete_manifest(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(.15)"])
        try:
            binding = self.binding(child)
            gate = RecordingGate()
            receipt = gate.start(OWNER, "cancel", str(uuid4()), lifetime=binding)
            snapshot = gate.active_lifetimes()[0]
            child.wait(timeout=5)
            reason = snapshot.probe.probe()
            self.assertEqual(reason, "client_exited")
            gate.stop_for_lifetime(snapshot, reason)
            final = gate.status(OWNER, receipt.capture_id)
            self.assertEqual(final.state, "draining")
            self.assertFalse(final.manifest_complete)
            self.assertIn("client_exited", final.gaps)
            self.assertEqual(gate.active_lifetimes(), ())
        finally:
            child.wait(timeout=5)

    def test_observed_cancel_only_and_normal_turn_does_not_stop(self):
        probe = LifetimeProbe(self.binding(), time.time_ns() + 10_000_000_000)
        self.append("turn_end", "a", stopReason="cancelled", boundarySource="recovered")
        self.append("turn_end", "a", stopReason="end_turn")
        self.append("turn_end", "a", stopReason="cancelled")
        self.assertIsNone(probe.probe())
        self.append("turn_start", "b")
        self.append("turn_end", "b", stopReason="cancelled")
        self.assertEqual(probe.probe(), "turn_cancelled")

    def test_old_snapshot_cannot_close_next_capture(self):
        gate = RecordingGate()
        first = gate.start(OWNER, "first", str(uuid4()), lifetime=self.binding())
        snapshot = gate.active_lifetimes()[0]
        gate.stop(OWNER, first.capture_id)
        gate.tick(now_ns=first.started_at_ns + 100_000_000_000)
        second = gate.start(OWNER, "second", str(uuid4()), lifetime=self.binding())
        self.assertIsNone(gate.stop_for_lifetime(snapshot, "client_exited"))
        self.assertEqual(gate.status(OWNER, second.capture_id).state, "recording")

    def test_retries_cannot_rebind_an_active_capture(self):
        gate = RecordingGate()
        request_id = str(uuid4())
        original = self.binding()
        first = gate.start(OWNER, "retry", request_id, lifetime=original)
        self.assertEqual(first.lifetime_protection, "server")
        self.assertEqual(gate.start(OWNER, "retry", request_id, lifetime=original), first)
        changed = original.model_copy(update={"offset": original.offset + 1})
        with self.assertRaisesRegex(ValueError, "binding"):
            gate.start(OWNER, "retry", request_id, lifetime=changed)
        self.assertEqual(len(gate.active_lifetimes()), 1)

    def test_receiver_restart_does_not_rearm_old_binding(self):
        ledger = self.root / "ledger.sqlite3"
        gate = RecordingGate(ledger_path=ledger)
        first = gate.start(OWNER, "restart", str(uuid4()), lifetime=self.binding())
        gate.close()
        restarted = RecordingGate(ledger_path=ledger)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.active_lifetimes(), ())
        self.assertEqual(restarted.status(OWNER, first.capture_id).state, "incomplete")
        self.assertIsNone(restarted.status(OWNER, first.capture_id).lifetime_protection)

    def test_binding_validator_rejects_arbitrary_executable_and_path(self):
        with patch("phoenix.server.recording_lifetime.Path.home", return_value=self.root):
            with self.assertRaisesRegex(ValueError, "executable"):
                validate_local_binding("kiro", SESSION, self.binding())
            with patch("phoenix.server.recording_lifetime.allowed_executable", return_value=True):
                self.assertEqual(
                    validate_local_binding("kiro", SESSION, self.binding()), self.binding()
                )
                (self.root / "secret").touch()
                invalid = self.binding().model_copy(
                    update={"transcript": str(self.root / "secret")}
                )
                with self.assertRaises(ValueError):
                    validate_local_binding("kiro", SESSION, invalid)

    def test_monotonic_deadline_survives_wall_clock_rollback(self):
        wall = time.time_ns()
        with (
            patch("phoenix.server.recording_lifetime.time.time_ns", return_value=wall),
            patch("phoenix.server.recording_lifetime.time.monotonic", return_value=10.0),
        ):
            probe = LifetimeProbe(self.binding(), wall + 1_000_000_000)
        with (
            patch(
                "phoenix.server.recording_lifetime.time.time_ns", return_value=wall - 60_000_000_000
            ),
            patch("phoenix.server.recording_lifetime.time.monotonic", return_value=11.1),
        ):
            self.assertEqual(probe.probe(), "hard_limit_expired")

    def test_reused_process_identity_is_rejected_before_binding(self):
        original = self.binding()
        invalid = original.model_copy(
            update={
                "owner": original.owner.model_copy(update={"created": original.owner.created - 10})
            }
        )
        with patch("phoenix.server.recording_lifetime.allowed_executable", return_value=True):
            with self.assertRaisesRegex(ValueError, "process identity"):
                validate_local_binding("kiro", SESSION, invalid)

    def test_malformed_and_oversized_rows_cannot_disable_cancellation(self):
        probe = LifetimeProbe(self.binding(), time.time_ns() + 10_000_000_000)
        with self.transcript.open("ab") as stream:
            stream.write(b'{"payload":null}\n' + b"x" * 800_000 + b"\n")
        self.append("turn_end", "a", stopReason="cancelled")
        self.assertEqual(probe.probe(), "turn_cancelled")


class LifetimeAsyncTests(LifetimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_failed_stop_does_not_disable_later_observations(self):
        gate = RecordingGate()
        receipt = gate.start(OWNER, "retry stop", str(uuid4()), lifetime=self.binding())
        stop = gate.stop_for_lifetime
        calls = 0

        def transient_failure(item, reason):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary failure")
            return stop(item, reason)

        self.append("turn_end", "a", stopReason="cancelled")
        with (
            patch.object(gate, "stop_for_lifetime", side_effect=transient_failure),
            self.assertLogs("phoenix.server.recording_lifetime", level="ERROR"),
        ):
            observer = asyncio.create_task(maintain_lifetimes(gate, poll_seconds=0.01))
            try:
                for _ in range(100):
                    if gate.status(OWNER, receipt.capture_id).state == "draining":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(gate.status(OWNER, receipt.capture_id).state, "draining")
                self.assertGreaterEqual(calls, 2)
            finally:
                observer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await observer

    async def test_lifecycle_loop_runs_while_other_readback_task_waits(self):
        binding = self.binding()
        gate = RecordingGate()
        receipt = gate.start(OWNER, "observer", str(uuid4()), lifetime=binding)
        waiting = asyncio.create_task(asyncio.sleep(20))
        observer = asyncio.create_task(maintain_lifetimes(gate, poll_seconds=0.01))
        self.append("turn_end", "a", stopReason="cancelled")
        try:
            for _ in range(100):
                if gate.status(OWNER, receipt.capture_id).state == "draining":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(gate.status(OWNER, receipt.capture_id).state, "draining")
            self.assertFalse(waiting.done())
        finally:
            for task in (observer, waiting):
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_loopback_api_acknowledges_guard_and_rejects_invalid_binding_before_start(self):
        app = FastAPI()
        app.state.recording_gate = gate = RecordingGate()
        app.include_router(router, prefix="/v1")
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        body = {
            "source": "kiro",
            "conversation_id": SESSION,
            "task": "api",
            "request_id": str(uuid4()),
            "lifetime": self.binding().model_dump(),
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            invalid = await client.post("/v1/recordings/start", json=body)
            self.assertEqual(invalid.status_code, 409)
            self.assertIsNone(gate.status(OWNER))
            with (
                patch("phoenix.server.recording_lifetime.Path.home", return_value=self.root),
                patch("phoenix.server.recording_lifetime.allowed_executable", return_value=True),
            ):
                response = await client.post("/v1/recordings/start", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["lifetime_protection"], "server")
            self.assertEqual(len(gate.active_lifetimes()), 1)


if __name__ == "__main__":
    unittest.main()
