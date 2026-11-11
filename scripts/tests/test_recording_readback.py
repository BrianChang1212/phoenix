import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from strawberry.relay import GlobalID

from phoenix.db import models
from phoenix.db.engines import aio_sqlite_engine
from phoenix.server.recording_readback import read_recording_spans
from phoenix.server.types import DbSessionFactory


class RecordingReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "phoenix.sqlite3"
        self.engine = aio_sqlite_engine(
            URL.create("sqlite+aiosqlite", database=str(db_path)), migrate=False
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(models.Base.metadata.create_all)
        session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = DbSessionFactory(session_factory, "sqlite")
        self.capture_id = str(uuid4())
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def add_span(
        self,
        *,
        project_name="task-recordings",
        session_id=None,
        capture_id=None,
        trace_id="01" * 16,
        span_id="02" * 8,
    ):
        async with self.db() as session:
            project = await session.scalar(
                select(models.Project).where(models.Project.name == project_name)
            )
            if project is None:
                project = models.Project(name=project_name, description=None)
                session.add(project)
                await session.flush()
            chosen_session_id = session_id or self.capture_id
            project_session = await session.scalar(
                select(models.ProjectSession).where(
                    models.ProjectSession.session_id == chosen_session_id
                )
            )
            if project_session is None:
                project_session = models.ProjectSession(
                    session_id=chosen_session_id,
                    project_id=project.id,
                    start_time=self.now,
                    end_time=self.now,
                )
                session.add(project_session)
                await session.flush()
            trace = models.Trace(
                project_rowid=project.id,
                project_session_rowid=project_session.id,
                trace_id=trace_id,
                start_time=self.now,
                end_time=self.now,
            )
            session.add(trace)
            await session.flush()
            session.add(
                models.Span(
                    trace_rowid=trace.id,
                    span_id=span_id,
                    parent_id=None,
                    name="operation",
                    span_kind="INTERNAL",
                    start_time=self.now,
                    end_time=self.now,
                    attributes={
                        "session": {"id": self.capture_id},
                        "recording": {
                            "capture_id": capture_id or self.capture_id,
                            "source": "codex",
                        },
                    },
                    events=[],
                    status_code="UNSET",
                    status_message="",
                    cumulative_error_count=0,
                    cumulative_llm_token_count_prompt=0,
                    cumulative_llm_token_count_completion=0,
                    llm_token_count_prompt=None,
                    llm_token_count_completion=None,
                )
            )
            await session.commit()

    async def test_all_expected_spans_read_back_with_real_relay_ids(self):
        first = ("01" * 16, "02" * 8)
        second = ("03" * 16, "04" * 8)
        await self.add_span(trace_id=first[0], span_id=first[1])
        await self.add_span(trace_id=second[0], span_id=second[1])

        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys={first, second},
        )

        self.assertEqual(result.persisted_span_keys, frozenset({first, second}))
        self.assertEqual(result.storage, "verified")
        self.assertEqual(
            result.project_relay_id,
            str(GlobalID("Project", str(result.project_rowid))),
        )
        self.assertEqual(
            result.session_relay_id,
            str(GlobalID("ProjectSession", str(result.session_rowid))),
        )

    async def test_partial_readback_keeps_only_verified_spans_and_real_session_ids(self):
        persisted = ("01" * 16, "02" * 8)
        missing = ("03" * 16, "04" * 8)
        await self.add_span(trace_id=persisted[0], span_id=persisted[1])

        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys={persisted, missing},
        )

        self.assertEqual(result.persisted_span_keys, frozenset({persisted}))
        self.assertEqual(result.storage, "partial")
        self.assertIsNotNone(result.project_relay_id)
        self.assertIsNotNone(result.session_relay_id)

    async def test_wrong_project_session_or_capture_provenance_is_not_verified(self):
        expected = ("01" * 16, "02" * 8)
        await self.add_span(
            project_name="other-project",
            trace_id=expected[0],
            span_id=expected[1],
        )

        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys={expected},
        )

        self.assertEqual(result.persisted_span_keys, frozenset())
        self.assertEqual(result.storage, "unverified")
        self.assertIsNone(result.project_relay_id)
        self.assertIsNone(result.session_relay_id)

    async def test_wrong_session_or_capture_provenance_is_not_verified(self):
        expected = ("01" * 16, "02" * 8)
        await self.add_span(
            session_id="another-capture",
            trace_id=expected[0],
            span_id=expected[1],
        )

        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys={expected},
        )

        self.assertEqual(result.persisted_span_keys, frozenset())
        self.assertEqual(result.storage, "unverified")
        self.assertIsNone(result.project_relay_id)
        self.assertIsNone(result.session_relay_id)

    async def test_wrong_capture_provenance_is_not_verified(self):
        expected = ("01" * 16, "02" * 8)
        await self.add_span(
            capture_id="another-capture",
            trace_id=expected[0],
            span_id=expected[1],
        )

        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys={expected},
        )

        self.assertEqual(result.persisted_span_keys, frozenset())
        self.assertEqual(result.storage, "unverified")
        self.assertIsNone(result.project_relay_id)
        self.assertIsNone(result.session_relay_id)

    async def test_empty_capture_has_no_session_link_or_token_estimate(self):
        result = await read_recording_spans(
            self.db,
            capture_id=self.capture_id,
            expected_span_keys=set(),
        )

        self.assertEqual(result.persisted_span_keys, frozenset())
        self.assertEqual(result.storage, "unverified")
        self.assertIsNone(result.project_relay_id)
        self.assertIsNone(result.session_relay_id)
        self.assertFalse(hasattr(result, "token_count_prompt"))


if __name__ == "__main__":
    unittest.main()
