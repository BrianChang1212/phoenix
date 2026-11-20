"""Serialize managed history deletion with ingestion and retain replay exclusions."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from phoenix.server.recording import RecordingGate

MANAGED_PROJECTS = frozenset({"task-recordings", "kiro-session-history"})


class HistoryCleanupPending(RuntimeError):
    pass


class HistoryDeletionGuard:
    def __init__(self, gate: RecordingGate, lock: asyncio.Lock) -> None:
        self.gate = gate
        self.lock = lock
        self._imports = 0
        self._marked: list[tuple[str, str]] = []

    @asynccontextmanager
    async def deleting(self) -> AsyncIterator[None]:
        async with self.lock:
            self._marked = []
            try:
                yield
            except BaseException:
                for project, session_id in self._marked:
                    self.gate.unmark_history_deleted(project, session_id)
                raise
            else:
                for project, session_id in self._marked:
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
        if not self.deleted(project, session_id):
            self._marked.append((project, session_id))
        self.gate.mark_history_deleted(project, session_id)

    def deleted(self, project: str, session_id: str) -> bool:
        return self.gate.history_deleted(project, session_id)
