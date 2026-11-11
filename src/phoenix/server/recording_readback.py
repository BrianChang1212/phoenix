"""Verify recording span keys against Phoenix's primary database."""

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import and_, or_, select
from strawberry.relay import GlobalID

from phoenix.db import models
from phoenix.server.recording import RECORDING_PROJECT
from phoenix.server.types import DbSessionFactory

SpanKey = tuple[str, str]


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
    # Preserve compatibility with a Phoenix adapter that has not unflattened dotted keys.
    recording_id = recording_id or attributes.get("recording.capture_id")
    session_id = session_id or attributes.get("session.id")
    return recording_id == capture_id and session_id == capture_id


async def read_recording_spans(
    db: DbSessionFactory,
    *,
    capture_id: str,
    expected_span_keys: set[SpanKey] | frozenset[SpanKey],
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
                    models.Project.name == RECORDING_PROJECT,
                    models.ProjectSession.session_id == capture_id,
                    key_filter,
                )
            )
            for row in await session.execute(stmt):
                project_id, session_id, trace_id, span_id, attributes = row
                key = (trace_id, span_id)
                if key not in expected or not _provenance_matches(attributes, capture_id):
                    continue
                # The joins and session filter prove all valid rows belong to the same project
                # and session. Preserve those exact rows for Relay ID construction below.
                project_rowid = project_id
                session_rowid = session_id
                persisted.add(key)

    if project_rowid is None or session_rowid is None:
        return RecordingReadback(expected, frozenset(persisted), "unverified")

    storage: Literal["partial", "verified"] = (
        "verified" if persisted == expected else "partial"
    )
    return RecordingReadback(
        expected_span_keys=expected,
        persisted_span_keys=frozenset(persisted),
        storage=storage,
        project_rowid=project_rowid,
        session_rowid=session_rowid,
        project_relay_id=str(GlobalID("Project", str(project_rowid))),
        session_relay_id=str(GlobalID("ProjectSession", str(session_rowid))),
    )
