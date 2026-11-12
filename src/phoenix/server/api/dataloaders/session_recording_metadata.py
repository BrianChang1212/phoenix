from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import and_, case, distinct, func, or_, select, true
from strawberry.dataloader import DataLoader

from phoenix.db import models
from phoenix.server.types import DbSessionFactory

Source = Literal["codex", "kiro"]
Key = int


@dataclass(frozen=True)
class SessionRecordingMetadata:
    task: str
    source: Source


Result = Optional[SessionRecordingMetadata]


class SessionRecordingMetadataDataLoader(DataLoader[Key, Result]):
    """Loads consistent recording provenance for sessions in one aggregate query."""

    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn, max_batch_size=200)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        attributes = models.Span.attributes
        nested_capture_id = attributes["recording"]["capture_id"].as_string()
        nested_task = attributes["recording"]["task"].as_string()
        nested_source = attributes["recording"]["source"].as_string()
        dotted_capture_id = attributes["recording.capture_id"].as_string()
        dotted_task = attributes["recording.task"].as_string()
        dotted_source = attributes["recording.source"].as_string()

        capture_id = func.coalesce(nested_capture_id, dotted_capture_id)
        task = func.coalesce(nested_task, dotted_task)
        source = func.coalesce(nested_source, dotted_source)
        has_claim = or_(capture_id.is_not(None), task.is_not(None), source.is_not(None))
        conflicting_shapes = or_(
            and_(
                nested_capture_id.is_not(None),
                dotted_capture_id.is_not(None),
                nested_capture_id != dotted_capture_id,
            ),
            and_(nested_task.is_not(None), dotted_task.is_not(None), nested_task != dotted_task),
            and_(
                nested_source.is_not(None),
                dotted_source.is_not(None),
                nested_source != dotted_source,
            ),
        )
        valid_claim = and_(
            capture_id == models.ProjectSession.session_id,
            task.is_not(None),
            func.length(task) <= 200,
            func.length(func.trim(task)) > 0,
            source.in_(("codex", "kiro")),
            ~conflicting_shapes,
        )
        invalid_claim = and_(has_claim, valid_claim.is_not(true()))

        stmt = (
            select(
                models.Trace.project_session_rowid.label("session_rowid"),
                func.sum(case((invalid_claim, 1), else_=0)).label("invalid_count"),
                func.count(distinct(task)).label("task_count"),
                func.count(distinct(source)).label("source_count"),
                func.min(task).label("task"),
                func.min(source).label("source"),
                func.max(case((has_claim, 1), else_=0)).label("has_claim"),
            )
            .join_from(models.Span, models.Trace)
            .join(
                models.ProjectSession,
                models.ProjectSession.id == models.Trace.project_session_rowid,
            )
            .where(models.Trace.project_session_rowid.in_(keys))
            .group_by(models.Trace.project_session_rowid)
        )

        results: dict[Key, SessionRecordingMetadata] = {}
        async with self._db.read() as session:
            async for session_rowid, invalid_count, task_count, source_count, task_value, source_value, has_claim_value in await session.stream(
                stmt
            ):
                if (
                    session_rowid is None
                    or not has_claim_value
                    or invalid_count
                    or task_count != 1
                    or source_count != 1
                    or source_value not in ("codex", "kiro")
                    or not isinstance(task_value, str)
                ):
                    continue
                results[session_rowid] = SessionRecordingMetadata(
                    task=task_value,
                    source=source_value,
                )
        return [results.get(key) for key in keys]
