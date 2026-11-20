"""Serialize managed history deletion with ingestion and retain replay exclusions."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Awaitable, Callable, Iterator, Optional

from sqlalchemy import select

from phoenix.db import models
from phoenix.server.recording import RecordingGate
from phoenix.server.types import DbSessionFactory

MANAGED_PROJECTS = frozenset({"task-recordings", "kiro-session-history"})


class HistoryCleanupPending(RuntimeError):
    pass


class HistoryDeletionGuard:
    def __init__(self, gate: RecordingGate, lock: asyncio.Lock) -> None:
        self.gate = gate
        self.lock = lock
        self._imports = 0
        self._marked: list[tuple[str, str, bool]] = []

    @asynccontextmanager
    async def deleting(
        self, retained: Optional[Callable[[str, str], Awaitable[bool]]] = None
    ) -> AsyncIterator[None]:
        async with self.lock:
            self._marked = []
            try:
                yield
            except asyncio.CancelledError:
                raise
            except BaseException:
                for project, session_id, previously_deleted in self._marked:
                    if previously_deleted or retained is None:
                        continue
                    try:
                        confirmed_present = await retained(project, session_id)
                    except Exception:
                        confirmed_present = False
                    if confirmed_present:
                        self.gate.unmark_history_deleted(project, session_id)
                raise
            else:
                for project, session_id, _ in self._marked:
                    try:
                        self.gate.finish_history_deletion(project, session_id)
                    except Exception as error:
                        raise HistoryCleanupPending(
                            "Session deleted, but recording receipt cleanup is pending. "
                            "Restart the receiver to retry cleanup."
                        ) from error
            finally:
                self._marked = []

    @contextmanager
    def importing(self) -> Iterator[None]:
        self._imports += 1
        try:
            yield
        finally:
            self._imports -= 1

    def check(self, project: str, session_id: str, queue_active: bool = False) -> None:
        if project == "task-recordings":
            self.gate.check_history_deletion(session_id)
        elif project == "kiro-session-history" and (self._imports or queue_active):
            raise ValueError("Kiro Session import is still queued or running")

    def mark(self, project: str, session_id: str) -> None:
        self._marked.append((project, session_id, self.deleted(project, session_id)))
        self.gate.mark_history_deleted(project, session_id)

    async def recover(self, db: DbSessionFactory) -> None:
        async with self.lock:
            for project, session_id in self.gate.pending_history_cleanups():
                async with db() as session:
                    existing = await session.scalar(
                        select(models.ProjectSession.id)
                        .join(models.Project, models.Project.id == models.ProjectSession.project_id)
                        .where(
                            models.Project.name == project,
                            models.ProjectSession.session_id == session_id,
                        )
                    )
                if existing is None:
                    self.gate.finish_history_deletion(project, session_id)

    def deleted(self, project: str, session_id: str) -> bool:
        return self.gate.history_deleted(project, session_id)
