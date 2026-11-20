"""Bounded server-owned retries for completed-turn Session imports."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException

ImportCallback = Callable[[], Awaitable[dict[str, Any]]]
ACTIVE_STATES = frozenset({"queued", "running"})


class _DeadlineExceeded(TimeoutError):
    pass


@dataclass
class _Job:
    callback: ImportCallback
    idle_deadline: float
    hard_deadline: float
    state: str = "queued"
    receipt: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task[None]] = None
    operation: Optional[asyncio.Task[dict[str, Any]]] = None

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "receipt": deepcopy(self.receipt), "error": self.error}


class KiroSessionQueue:
    """Keep import work alive independently of the client that submitted it.

    The API owns Session authorization and supplies a captured, validated import
    callback. All methods run on the owning server event loop.
    """

    def __init__(
        self,
        *,
        idle_timeout: float = 30.0,
        retry_interval: float = 0.5,
        hard_timeout: float = 2 * 60 * 60,
        max_jobs: int = 128,
    ) -> None:
        if min(idle_timeout, retry_interval, hard_timeout) <= 0:
            raise ValueError("Session queue deadlines must be positive")
        if not 1 <= max_jobs <= 128:
            raise ValueError("Session queue capacity must be between 1 and 128")
        self._idle_timeout = idle_timeout
        self._retry_interval = retry_interval
        self._hard_timeout = hard_timeout
        self._max_jobs = max_jobs
        self._jobs: dict[tuple[str, str], _Job] = {}
        self._closed = False

    def submit(self, session_id: str, turn_id: str, run_import: ImportCallback) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Session queue is closed")
        loop = asyncio.get_running_loop()
        now = loop.time()
        key = session_id, turn_id
        existing = self._jobs.get(key)
        if existing is not None:
            if existing.state in ACTIVE_STATES:
                if now < min(existing.idle_deadline, existing.hard_deadline):
                    existing.idle_deadline = min(now + self._idle_timeout, existing.hard_deadline)
                return existing.status()
            if existing.state not in ("failed", "timed_out"):
                return existing.status()
            del self._jobs[key]
        if len(self._jobs) >= self._max_jobs:
            terminal = next(
                (k for k, job in self._jobs.items() if job.state not in ACTIVE_STATES), None
            )
            if terminal is None:
                raise RuntimeError("Session queue capacity reached")
            del self._jobs[terminal]
        job = _Job(run_import, now + self._idle_timeout, now + self._hard_timeout)
        self._jobs[key] = job
        job.task = loop.create_task(self._run(job))
        return job.status()

    @property
    def active(self) -> bool:
        return any(job.state in ACTIVE_STATES for job in self._jobs.values())

    def status(self, session_id: str, turn_id: str) -> Optional[dict[str, Any]]:
        job = self._jobs.get((session_id, turn_id))
        return job.status() if job is not None else None

    async def _invoke(self, job: _Job) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        operation = loop.create_task(job.callback())
        job.operation = operation
        try:
            while True:
                remaining = min(job.idle_deadline, job.hard_deadline) - loop.time()
                if remaining <= 0:
                    raise _DeadlineExceeded
                done, _ = await asyncio.wait(
                    {operation}, timeout=min(self._retry_interval, remaining)
                )
                if done:
                    return operation.result()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            job.operation = None

    async def _run(self, job: _Job) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                deadline = min(job.idle_deadline, job.hard_deadline)
                if loop.time() >= deadline:
                    job.state, job.error = "timed_out", "deadline_exceeded"
                    return
                job.state = "running"
                receipt = None
                received = False
                try:
                    receipt = await self._invoke(job)
                    received = True
                except HTTPException as error:
                    if error.status_code != 409:
                        job.state, job.error = "failed", "httpexception"
                        return
                    job.error = "waiting_for_completed_turn"
                except _DeadlineExceeded:
                    job.state, job.error = "timed_out", "deadline_exceeded"
                    return
                except Exception as error:
                    job.state, job.error = "failed", type(error).__name__.lower()[:128]
                    return
                if received:
                    if not isinstance(receipt, dict):
                        job.state, job.error = "failed", "invalid_import_receipt"
                        return
                    job.receipt = deepcopy(receipt)
                    storage = receipt.get("storage")
                    gaps = receipt.get("gaps")
                    coverage = receipt.get("coverage")
                    if storage == "verified":
                        if (
                            receipt.get("state") == "imported"
                            and gaps == []
                            and isinstance(coverage, dict)
                            and coverage.get("complete") is True
                        ):
                            job.state, job.error = "completed", None
                        elif (
                            gaps or isinstance(coverage, dict) and coverage.get("complete") is False
                        ):
                            job.state, job.error = "incomplete", None
                        else:
                            job.state, job.error = "failed", "invalid_import_receipt"
                        return
                    if storage not in ("partial", "unverified"):
                        job.state, job.error = "failed", "invalid_import_receipt"
                        return
                    job.error = "waiting_for_verified_storage"
                remaining = min(job.idle_deadline, job.hard_deadline) - loop.time()
                if remaining > 0:
                    await asyncio.sleep(min(self._retry_interval, remaining))
        except asyncio.CancelledError:
            job.state, job.error = "failed", "queue_closed"
            raise

    async def close(self) -> None:
        self._closed = True
        tasks = []
        for job in self._jobs.values():
            if job.task is not None and not job.task.done():
                job.state, job.error = "failed", "queue_closed"
                job.task.cancel()
                tasks.append(job.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
