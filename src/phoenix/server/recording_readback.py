import asyncio
from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import and_, or_, select
from strawberry.relay import GlobalID

from phoenix.db import models
from phoenix.server.recording import RECORDING_PROJECT
from phoenix.server.types import DbSessionFactory

SpanKey = tuple[str, str]


async def settle_recording_spans(
    db: DbSessionFactory,
    *,
    capture_id: str,
    expected_span_keys: set[SpanKey] | frozenset[SpanKey],
    project_name: str = RECORDING_PROJECT,
    timeout_sec: float = 5.0,
    retry_interval_sec: float = 0.1,
) -> "RecordingReadback":
    """Re-read concurrent commits within one budget, preserving the last complete read."""
    if timeout_sec <= 0 or retry_interval_sec <= 0:
        raise ValueError("Readback deadline and interval must be positive")
    expected = frozenset(expected_span_keys)
    deadline = asyncio.get_running_loop().time() + timeout_sec
    latest: Optional[RecordingReadback] = None
    try:
        async with asyncio.timeout(timeout_sec):
            while True:
                latest = await read_recording_spans(
                    db, capture_id=capture_id, expected_span_keys=expected,
                    project_name=project_name,
                )
                if latest.storage == "verified" or not expected:
                    return latest
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(retry_interval_sec, remaining))
    except TimeoutError:
        if latest is None:
            raise
    assert latest is not None
    return latest


@dataclass(frozen=True)
class RecordingReadback:
    expected_span_keys: frozenset[SpanKey]
    persisted_span_keys: frozenset[SpanKey]
    storage: Literal["unverified", "partial", "verified"]
    project_rowid: Optional[int] = None
    session_rowid: Optional[int] = None
    project_relay_id: Optional[str] = None
    session_relay_id: Optional[str] = None


def _provenance_matches(attributes: object, capture_id: str) -> bool:
    if not isinstance(attributes, dict):
        return False
    recording = attributes.get("recording")
    session = attributes.get("session")
    recording_id = recording.get("capture_id") if isinstance(recording, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    recording_id = recording_id or attributes.get("recording.capture_id")
    session_id = session_id or attributes.get("session.id")
    return recording_id == capture_id and session_id == capture_id


async def read_recording_spans(
    db: DbSessionFactory,
    *,
    capture_id: str,
    expected_span_keys: set[SpanKey] | frozenset[SpanKey],
    project_name: str = RECORDING_PROJECT,
) -> RecordingReadback:
    """Return only expected spans durably attached to this recording's real Session.

    Reads use the primary database session factory. The read replica can be arbitrarily
    stale, so it cannot prove that an accepted span was committed.
    """
    expected = frozenset(expected_span_keys)
    if not expected:
        return RecordingReadback(expected, frozenset(), "unverified")

    persisted: set[SpanKey] = set()
    project_rowid: Optional[int] = None
    session_rowid: Optional[int] = None
    # Keep each disjunction below conservative SQLite and Postgres bind parameter limits.
    keys = tuple(expected)
    async with db() as session:
        for offset in range(0, len(keys), 250):
            chunk = keys[offset : offset + 250]
            key_filter = or_(
                *(
                    and_(models.Trace.trace_id == trace_id, models.Span.span_id == span_id)
                    for trace_id, span_id in chunk
                )
            )
            stmt = (
                select(
                    models.Project.id,
                    models.ProjectSession.id,
                    models.Trace.trace_id,
                    models.Span.span_id,
                    models.Span.attributes,
                )
                .join(
                    models.ProjectSession,
                    models.ProjectSession.project_id == models.Project.id,
                )
                .join(
                    models.Trace,
                    and_(
                        models.Trace.project_rowid == models.Project.id,
                        models.Trace.project_session_rowid == models.ProjectSession.id,
                    ),
                )
                .join(models.Span, models.Span.trace_rowid == models.Trace.id)
                .where(
                    models.Project.name == project_name,
                    models.ProjectSession.session_id == capture_id,
                    key_filter,
                )
            )
            for row in await session.execute(stmt):
                project_id, session_id, trace_id, span_id, attributes = row
                key = (trace_id, span_id)
                if key not in expected or not _provenance_matches(attributes, capture_id):
                    continue
                project_rowid = project_id
                session_rowid = session_id
                persisted.add(key)

    if project_rowid is None or session_rowid is None:
        return RecordingReadback(expected, frozenset(persisted), "unverified")

    storage: Literal["partial", "verified"] = "verified" if persisted == expected else "partial"
    return RecordingReadback(
        expected_span_keys=expected,
        persisted_span_keys=frozenset(persisted),
        storage=storage,
        project_rowid=project_rowid,
        session_rowid=session_rowid,
        project_relay_id=str(GlobalID("Project", str(project_rowid))),
        session_relay_id=str(GlobalID("ProjectSession", str(session_rowid))),
    )
