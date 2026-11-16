import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

MODULE = Path(__file__).resolve().parents[2] / "src/phoenix/server/kiro_session_queue.py"
spec = importlib.util.spec_from_file_location("kiro_session_queue_test_target", MODULE)
queue_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = queue_module
spec.loader.exec_module(queue_module)


def success():
    return {"state": "imported", "storage": "verified", "gaps": [], "coverage": {"complete": True}}


class KiroSessionQueueTests(unittest.IsolatedAsyncioTestCase):
    def queue(self, **kwargs):
        queue = queue_module.KiroSessionQueue(**kwargs)
        self.addAsyncCleanup(queue.close)
        return queue

    async def finished(self, queue, session="session", turn="turn"):
        async with asyncio.timeout(1):
            while True:
                result = queue.status(session, turn)
                if result is not None and result["state"] not in ("queued", "running"):
                    return result
                await asyncio.sleep(0.002)

    async def test_producer_return_does_not_cancel_the_server_job(self):
        queue = self.queue(idle_timeout=0.2, retry_interval=0.005)

        async def importer():
            await asyncio.sleep(0.01)
            return success()

        async def producer():
            return queue.submit("session", "turn", importer)

        initial = await producer()
        self.assertEqual(initial["state"], "queued")
        final = await self.finished(queue)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["receipt"], success())

    async def test_duplicate_submit_keeps_one_job_and_refreshes_a_running_deadline(self):
        queue = self.queue(idle_timeout=0.06, retry_interval=0.005)
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def importer():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return success()

        queue.submit("session", "turn", importer)
        await started.wait()
        await asyncio.sleep(0.035)
        queue.submit("session", "turn", importer)
        await asyncio.sleep(0.035)
        self.assertEqual(queue.status("session", "turn")["state"], "running")
        release.set()
        self.assertEqual((await self.finished(queue))["state"], "completed")
        self.assertEqual(calls, 1)

    async def test_source_turn_completion_409_is_retried(self):
        queue = self.queue(idle_timeout=0.2, retry_interval=0.005)
        calls = 0

        async def importer():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise HTTPException(409, "PRIVATE_DETAIL")
            return success()

        queue.submit("session", "turn", importer)
        result = await self.finished(queue)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(calls, 3)
        self.assertNotIn("PRIVATE_DETAIL", repr(result))

    async def test_inactivity_deadline_bounds_retries(self):
        queue = self.queue(idle_timeout=0.035, retry_interval=0.005)

        async def importer():
            raise HTTPException(409, "PRIVATE_DETAIL")

        queue.submit("session", "turn", importer)
        result = await self.finished(queue)
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["error"], "deadline_exceeded")

    async def test_hard_deadline_cannot_be_extended_by_duplicate_submit(self):
        queue = self.queue(idle_timeout=0.04, hard_timeout=0.07, retry_interval=0.005)
        blocked = asyncio.Event()

        async def importer():
            await blocked.wait()
            return success()

        queue.submit("session", "turn", importer)
        for _ in range(5):
            await asyncio.sleep(0.02)
            queue.submit("session", "turn", importer)
        self.assertEqual((await self.finished(queue))["state"], "timed_out")

    async def test_partial_storage_retries_until_verified(self):
        queue = self.queue(idle_timeout=0.2, retry_interval=0.005)
        calls = 0

        async def importer():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"state": "incomplete", "storage": "partial", "gaps": ["missing_persisted_spans"], "coverage": {"complete": True}}
            return success()

        queue.submit("session", "turn", importer)
        self.assertEqual((await self.finished(queue))["state"], "completed")
        self.assertEqual(calls, 2)

    async def test_verified_gaps_finish_as_incomplete_without_retry(self):
        queue = self.queue(idle_timeout=0.2, retry_interval=0.005)
        calls = 0

        async def importer():
            nonlocal calls
            calls += 1
            return {"state": "incomplete", "storage": "verified", "gaps": ["unknown_event_type"], "coverage": {"complete": False}}

        queue.submit("session", "turn", importer)
        self.assertEqual((await self.finished(queue))["state"], "incomplete")
        self.assertEqual(calls, 1)

    async def test_errors_only_retain_the_exception_type(self):
        queue = self.queue()

        async def importer():
            raise RuntimeError("PRIVATE_DETAIL")

        queue.submit("session", "turn", importer)
        final = await self.finished(queue)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["error"], "runtimeerror")
        self.assertNotIn("PRIVATE_DETAIL", repr(final))

    async def test_close_cancels_owned_tasks_and_prevents_new_submission(self):
        queue = self.queue(idle_timeout=1)
        cancelled = asyncio.Event()

        async def importer():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        queue.submit("session", "turn", importer)
        await asyncio.sleep(0)
        await queue.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(queue.status("session", "turn")["error"], "queue_closed")
        with self.assertRaises(RuntimeError):
            queue.submit("another", "turn", importer)

    async def test_resource_bound_evicts_only_terminal_jobs(self):
        queue = self.queue(max_jobs=1, idle_timeout=1)
        release = asyncio.Event()

        async def importer():
            await release.wait()
            return success()

        queue.submit("session", "turn", importer)
        with self.assertRaises(RuntimeError):
            queue.submit("another", "turn", importer)
        release.set()
        await self.finished(queue)
        queue.submit("another", "turn", importer)
        self.assertIsNone(queue.status("session", "turn"))
        self.assertEqual((await self.finished(queue, "another"))["state"], "completed")

    async def test_status_is_a_copy_and_unknown_job_is_none(self):
        queue = self.queue()

        async def importer():
            return success()

        self.assertIsNone(queue.status("unknown", "unknown"))
        queue.submit("session", "turn", importer)
        result = await self.finished(queue)
        result["receipt"]["gaps"].append("modified")
        self.assertEqual(queue.status("session", "turn")["receipt"]["gaps"], [])

    async def test_timed_out_job_can_be_restarted_with_a_new_callback(self):
        queue = self.queue(idle_timeout=0.025, retry_interval=0.005)

        async def not_ready():
            raise HTTPException(409, "not finished")

        async def ready():
            return success()

        queue.submit("session", "turn", not_ready)
        self.assertEqual((await self.finished(queue))["state"], "timed_out")
        self.assertEqual(queue.submit("session", "turn", ready)["state"], "queued")
        self.assertEqual((await self.finished(queue))["state"], "completed")


if __name__ == "__main__":
    unittest.main()
