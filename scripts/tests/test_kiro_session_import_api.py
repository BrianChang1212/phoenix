import unittest
import asyncio
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from phoenix.server.api.routers.v1.recordings import router
from phoenix.server.recording import RecordingGate


class KiroSessionImportAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.state.recording_gate = RecordingGate()
        self.app.state.db = object()
        self.app.include_router(router, prefix="/v1")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:6006",
        )
        self.body = {"session_id": "sess_a2150126-9bcd-4987-9934-bd49db63d0bf"}

    async def asyncTearDown(self):
        queue = getattr(self.app.state, "kiro_session_queue", None)
        if queue is not None:
            await queue.close()
        await self.client.aclose()
        self.app.state.recording_gate.close()

    async def test_browser_origin_cannot_import_local_session(self):
        response = await self.client.post(
            "/v1/recordings/kiro-session/import", json=self.body,
            headers={"origin": "http://untrusted.example"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_remote_client_cannot_import_local_session(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("192.0.2.1", 1234)),
            base_url="http://127.0.0.1:6006",
        ) as client:
            response = await client.post("/v1/recordings/kiro-session/import", json=self.body)
        self.assertEqual(response.status_code, 403)

    async def test_client_cannot_select_transcript_path_or_supply_spans(self):
        for extra in ({"path": "C:/private"}, {"spans": []}):
            response = await self.client.post(
                "/v1/recordings/kiro-session/import", json={**self.body, **extra}
            )
            self.assertEqual(response.status_code, 422)

    async def test_invalid_session_id_is_rejected(self):
        response = await self.client.post(
            "/v1/recordings/kiro-session/import", json={"session_id": "../other"}
        )
        self.assertEqual(response.status_code, 422)

    async def test_only_server_parsed_spans_are_ingested_and_read_back(self):
        ident = str(uuid4())
        imported = SimpleNamespace(
            import_id=ident, request=object(), expected_span_keys=frozenset({("aa", "bb")}),
            coverage={"logical_steps": 1}, gaps=(), through_record_id="last-record",
        )
        readback = SimpleNamespace(
            storage="verified", persisted_span_keys=imported.expected_span_keys,
            project_relay_id="Project:1", session_relay_id="Session:1",
        )
        with patch("phoenix.server.kiro_session_history.load_snapshot", return_value=SimpleNamespace(sha256="a" * 64)) as load, \
             patch("phoenix.server.kiro_session_history.build_import", return_value=imported), \
             patch("phoenix.server.api.routers.v1.traces._add_spans", new_callable=AsyncMock) as ingest, \
             patch("phoenix.server.recording_readback.settle_recording_spans", new_callable=AsyncMock, return_value=readback) as settle:
            response = await self.client.post(
                "/v1/recordings/kiro-session/import",
                json={**self.body, "through_turn_id": "turn-1"},
            )
        self.assertEqual(response.status_code, 200)
        receipt = response.json()
        self.assertEqual(receipt["state"], "imported")
        self.assertEqual(receipt["storage"], "verified")
        self.assertEqual(receipt["gaps"], [])
        load.assert_called_once_with(self.body["session_id"], through_turn_id="turn-1")
        self.assertIs(ingest.await_args.args[0], imported.request)
        self.assertFalse(ingest.await_args.kwargs["recording_enabled"])
        self.assertEqual(settle.await_args.kwargs["project_name"], "kiro-session-history")

    async def test_large_import_gets_a_readback_deadline_that_scales_with_its_span_count(self):
        keys = frozenset((f"{i:016x}", f"{i:016x}") for i in range(2535))
        imported = SimpleNamespace(
            import_id=str(uuid4()), request=object(), expected_span_keys=keys,
            coverage={"logical_steps": 1}, gaps=(), through_record_id="last-record",
        )
        readback = SimpleNamespace(
            storage="verified", persisted_span_keys=keys,
            project_relay_id="Project:1", session_relay_id="Session:1",
        )
        with patch("phoenix.server.kiro_session_history.load_snapshot", return_value=SimpleNamespace(sha256="a" * 64)), \
             patch("phoenix.server.kiro_session_history.build_import", return_value=imported), \
             patch("phoenix.server.api.routers.v1.traces._add_spans", new_callable=AsyncMock), \
             patch("phoenix.server.recording_readback.settle_recording_spans", new_callable=AsyncMock, return_value=readback) as settle:
            response = await self.client.post("/v1/recordings/kiro-session/import", json=self.body)
        self.assertEqual(response.status_code, 200)
        # Ingestion runs near 160 spans/s here: a fixed 5 s window verified only ~800 of 2535 spans.
        self.assertGreaterEqual(settle.await_args.kwargs["timeout_sec"], 20.0)
        self.assertLessEqual(settle.await_args.kwargs["timeout_sec"], 60.0)
    async def test_partial_storage_never_claims_complete_import(self):
        imported = SimpleNamespace(
            import_id=str(uuid4()), request=object(), expected_span_keys=frozenset({("aa", "bb")}),
            coverage={}, gaps=(), through_record_id="last-record",
        )
        readback = SimpleNamespace(
            storage="partial", persisted_span_keys=frozenset(),
            project_relay_id=None, session_relay_id=None,
        )
        with patch("phoenix.server.kiro_session_history.load_snapshot", return_value=SimpleNamespace(sha256="a" * 64)), \
             patch("phoenix.server.kiro_session_history.build_import", return_value=imported), \
             patch("phoenix.server.api.routers.v1.traces._add_spans", new_callable=AsyncMock), \
             patch("phoenix.server.recording_readback.settle_recording_spans", new_callable=AsyncMock, return_value=readback):
            response = await self.client.post("/v1/recordings/kiro-session/import", json=self.body)
        receipt = response.json()
        self.assertEqual(receipt["state"], "incomplete")
        self.assertIn("storage_not_verified", receipt["gaps"])
        self.assertIsNone(receipt["timeline_url"])

    async def test_queue_handoff_returns_before_source_turn_finishes(self):
        finished = asyncio.Event()
        receipt = {"state": "imported", "storage": "verified", "gaps": [],
                   "coverage": {"complete": True}, "import_id": str(uuid4())}

        async def import_after_turn(body, request):
            await finished.wait()
            return receipt

        body = {**self.body, "through_turn_id": "turn-1"}
        with patch("phoenix.server.api.routers.v1.recordings.import_kiro_session", side_effect=import_after_turn):
            response = await asyncio.wait_for(
                self.client.post("/v1/recordings/kiro-session/queue", json=body), timeout=1,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(response.json()["state"], ("queued", "running"))
            finished.set()
            for _ in range(20):
                status = await self.client.post("/v1/recordings/kiro-session/queue/status", json=body)
                if status.json().get("state") == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(status.json()["receipt"], receipt)

    async def test_queue_requires_explicit_turn_boundary(self):
        response = await self.client.post("/v1/recordings/kiro-session/queue", json=self.body)
        self.assertEqual(response.status_code, 422)

    async def test_browser_cannot_queue_local_import(self):
        response = await self.client.post(
            "/v1/recordings/kiro-session/queue", json={**self.body, "through_turn_id": "turn-1"},
            headers={"origin": "http://untrusted.example"},
        )
        self.assertEqual(response.status_code, 403)
