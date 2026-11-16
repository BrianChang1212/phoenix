import asyncio
import importlib
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from phoenix.server.kiro_session_queue import KiroSessionQueue

app_module = importlib.import_module("phoenix.server.app")


class SessionQueueLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_stops_import_callbacks_before_closing_storage(self):
        events = []
        started = asyncio.Event()

        @asynccontextmanager
        async def resource(name, value=None):
            try:
                yield value
            finally:
                events.append(name)

        async def importing():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("callback-ended")

        queue = KiroSessionQueue()
        app = FastAPI()
        app.state.kiro_session_queue = queue
        class Settings:
            bootstrap = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                events.append("settings")

        lifespan = app_module._lifespan(
            db=object(), bulk_inserter=resource("bulk", (AsyncMock(), AsyncMock(), AsyncMock())),
            dml_event_handler=resource("dml"), trace_data_sweeper=None,
            agent_session_sweeper=resource("agent-sweeper"),
            experiment_sweeper=resource("experiment-sweeper"),
            generative_model_store=resource("model-store"), model_cost_manifest_fetcher=None,
            system_settings=Settings(), db_disk_usage_monitor=resource("disk"),
            experiment_runner=resource("runner"), sandbox_session_manager=resource("sandbox"),
            sandbox_runtime=SimpleNamespace(monty=resource("monty")), grpc_port=14339,
            grpc_host="127.0.0.1",
        )
        try:
            with patch.object(app_module, "load_deployment_seed", new_callable=AsyncMock, return_value=b"x" * 32), \
                 patch.object(app_module, "GrpcServer", return_value=resource("grpc")), \
                 patch.object(app_module, "CapacityInterceptor", return_value=object()), \
                 patch.object(app_module, "user_grpc_interceptors", return_value=[]), \
                 patch.object(app_module, "get_env_allowed_sandbox_providers", return_value=set()):
                async with lifespan(app):
                    queue.submit("session", "turn", importing)
                    await started.wait()
            self.assertIn("callback-ended", events)
            self.assertLess(events.index("callback-ended"), events.index("bulk"))
            with self.assertRaises(RuntimeError):
                queue.submit("session", "new-turn", importing)
        finally:
            await queue.close()
