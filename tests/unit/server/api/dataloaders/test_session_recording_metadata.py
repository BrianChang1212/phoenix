from datetime import datetime, timezone

from phoenix.server.api.dataloaders.session_recording_metadata import (
    SessionRecordingMetadataDataLoader,
)
from phoenix.server.types import DbSessionFactory

from ...._helpers import _add_project, _add_project_session, _add_span, _add_trace


async def test_session_recording_metadata_uses_nested_child_span_attributes(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-1", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(
            session,
            trace,
            attributes={
                "recording": {
                    "capture_id": "capture-1",
                    "task": "Review Phoenix trace",
                    "source": "codex",
                }
            },
            parent_span=await _add_span(session, trace, attributes={"name": "parent"}),
        )
        await session.flush()
        session_id = project_session.id

    metadata = await SessionRecordingMetadataDataLoader(db).load(session_id)

    assert metadata is not None
    assert metadata.task == "Review Phoenix trace"
    assert metadata.source == "codex"


async def test_session_recording_metadata_accepts_literal_dotted_attributes(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-2", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(
            session,
            trace,
            attributes={
                "recording.capture_id": "capture-2",
                "recording.task": "Investigate latency",
                "recording.source": "kiro",
            },
        )
        await session.flush()
        session_id = project_session.id

    metadata = await SessionRecordingMetadataDataLoader(db).load(session_id)

    assert metadata is not None
    assert metadata.task == "Investigate latency"
    assert metadata.source == "kiro"


async def test_session_recording_metadata_rejects_conflicting_or_wrong_capture(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-3", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(
            session,
            trace,
            attributes={
                "recording": {
                    "capture_id": "capture-3",
                    "task": "First task",
                    "source": "codex",
                }
            },
        )
        await _add_span(
            session,
            trace,
            attributes={
                "recording.capture_id": "other-capture",
                "recording.task": "First task",
                "recording.source": "codex",
            },
        )
        await session.flush()
        session_id = project_session.id

    assert await SessionRecordingMetadataDataLoader(db).load(session_id) is None


async def test_session_recording_metadata_rejects_conflicting_task_claims(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-4", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        for task in ("First task", "Second task"):
            await _add_span(
                session,
                trace,
                attributes={
                    "recording": {
                        "capture_id": "capture-4",
                        "task": task,
                        "source": "codex",
                    }
                },
            )
        await session.flush()
        session_id = project_session.id

    assert await SessionRecordingMetadataDataLoader(db).load(session_id) is None


async def test_session_recording_metadata_rejects_partial_claim_alongside_valid_claim(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-partial", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(
            session,
            trace,
            attributes={
                "recording": {
                    "capture_id": "capture-partial",
                    "task": "Consistent task",
                    "source": "codex",
                }
            },
        )
        await _add_span(
            session,
            trace,
            attributes={
                "recording": {
                    "task": "Consistent task",
                    "source": "codex",
                }
            },
        )
        await session.flush()
        session_id = project_session.id

    assert await SessionRecordingMetadataDataLoader(db).load(session_id) is None


async def test_session_recording_metadata_rejects_conflicting_key_shapes(
    db: DbSessionFactory,
) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, session_id="capture-5", start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(
            session,
            trace,
            attributes={
                "recording": {"capture_id": "capture-5", "task": "Task", "source": "codex"},
                "recording.capture_id": "capture-5",
                "recording.task": "Different task",
                "recording.source": "codex",
            },
        )
        await session.flush()
        session_id = project_session.id

    assert await SessionRecordingMetadataDataLoader(db).load(session_id) is None


async def test_ordinary_session_has_no_recording_metadata(db: DbSessionFactory) -> None:
    async with db() as session:
        project = await _add_project(session)
        project_session = await _add_project_session(
            session, project, start_time=datetime.now(timezone.utc)
        )
        trace = await _add_trace(session, project, project_session)
        await _add_span(session, trace, attributes={"input": {"value": "hello"}})
        await session.flush()
        session_id = project_session.id

    assert await SessionRecordingMetadataDataLoader(db).load(session_id) is None
