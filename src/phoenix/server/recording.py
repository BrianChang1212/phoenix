"""Bounded, process-local admission for explicitly recorded conversations."""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import time_ns
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span

Source = Literal["codex", "kiro"]
RECORDING_PROJECT = "task-recordings"


@dataclass(frozen=True)
class ConversationKey:
    source: Source
    conversation_id: str

    def __post_init__(self) -> None:
        if self.source not in ("codex", "kiro"):
            raise ValueError("Unknown recording source")
        if not self.conversation_id.strip() or len(self.conversation_id) > 200:
            raise ValueError("A current conversation identity is required")
        if self.source == "codex":
            UUID(self.conversation_id)


@dataclass(frozen=True)
class CaptureReceipt:
    capture_id: str
    source: Source
    conversation_id: str
    task: str
    state: Literal["recording", "draining", "observed", "incomplete", "empty", "failed"]
    started_at_ns: int
    stopped_at_ns: Optional[int]
    idle_lease_deadline_ns: int
    hard_deadline_ns: int
    drain_deadline_ns: Optional[int]
    queued_spans: int
    persisted_spans: int
    gaps: tuple[str, ...]
    session_id: str
    storage: Literal["unverified", "partial", "verified"] = "unverified"
    project_name: str = RECORDING_PROJECT
    timeline_url: Optional[str] = None
    manifest_complete: bool = True


@dataclass
class _Capture:
    owner: ConversationKey
    task: str
    request_id: str
    capture_id: str = field(default_factory=lambda: str(uuid4()))
    started_at_ns: int = field(default_factory=time_ns)
    stopped_at_ns: Optional[int] = None
    idle_lease_deadline_ns: int = 0
    hard_deadline_ns: int = 0
    drain_deadline_ns: Optional[int] = None
    persisted_spans: int = 0
    state: Literal["recording", "draining", "observed", "incomplete", "empty", "failed"] = "recording"
    admitted: set[tuple[bytes, bytes]] = field(default_factory=set)
    gaps: set[str] = field(default_factory=set)
    storage: Literal["unverified", "partial", "verified"] = "unverified"
    timeline_url: Optional[str] = None
    observed_event_ids: set[str] = field(default_factory=set)
    manifest_complete: bool = True

    def receipt(self) -> CaptureReceipt:
        return CaptureReceipt(
            capture_id=self.capture_id,
            source=self.owner.source,
            conversation_id=self.owner.conversation_id,
            task=self.task,
            state=self.state,
            started_at_ns=self.started_at_ns,
            stopped_at_ns=self.stopped_at_ns,
            idle_lease_deadline_ns=self.idle_lease_deadline_ns,
            hard_deadline_ns=self.hard_deadline_ns,
            drain_deadline_ns=self.drain_deadline_ns,
            queued_spans=len(self.admitted),
            persisted_spans=self.persisted_spans,
            gaps=tuple(sorted(self.gaps)),
            session_id=self.capture_id,
            storage=self.storage,
            timeline_url=self.timeline_url,
            manifest_complete=self.manifest_complete,
        )


class _RecordingLedger:
    """Persist bounded receipt metadata and span identities, never raw spans."""

    _SCHEMA_VERSION = 3

    def __init__(self, path: Path, binding_id: Optional[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = path.with_suffix(path.suffix + ".lock").open("a+b")
        if self._lock_file.seek(0, os.SEEK_END) == 0:
            self._lock_file.write(b"0")
            self._lock_file.flush()
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._lock_file.close()
            raise RuntimeError("Recording ledger is already owned by another receiver") from error
        try:
            self._db = sqlite3.connect(path, timeout=2)
            self._initialize(binding_id)
        except Exception:
            self.close()
            raise

    def _initialize(self, binding_id: Optional[str]) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, self._SCHEMA_VERSION):
            raise RuntimeError("Unsupported recording ledger schema")
        if version == 0:
            with self._db:
                self._db.execute(
                    "CREATE TABLE captures ("
                    "capture_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                    "conversation_id TEXT NOT NULL, task TEXT NOT NULL, request_id TEXT NOT NULL, "
                    "state TEXT NOT NULL, started_at_ns INTEGER NOT NULL, stopped_at_ns INTEGER, "
                    "idle_lease_deadline_ns INTEGER NOT NULL, hard_deadline_ns INTEGER NOT NULL, "
                    "drain_deadline_ns INTEGER, persisted_spans INTEGER NOT NULL DEFAULT 0, "
                    "storage TEXT NOT NULL DEFAULT 'unverified', timeline_url TEXT, "
                    "observed_event_ids TEXT NOT NULL DEFAULT '[]', "
                    "manifest_complete INTEGER NOT NULL DEFAULT 1, "
                    "gaps TEXT NOT NULL, UNIQUE(source, conversation_id, request_id))"
                )
                self._db.execute(
                    "CREATE TABLE span_keys (capture_id TEXT NOT NULL, trace_id BLOB NOT NULL, "
                    "span_id BLOB NOT NULL, PRIMARY KEY(capture_id, trace_id, span_id))"
                )
                self._db.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
        elif version == 1:
            with self._db:
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN idle_lease_deadline_ns INTEGER NOT NULL DEFAULT 0"
                )
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN hard_deadline_ns INTEGER NOT NULL DEFAULT 0"
                )
                self._db.execute("ALTER TABLE captures ADD COLUMN drain_deadline_ns INTEGER")
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN persisted_spans INTEGER NOT NULL DEFAULT 0"
                )
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN storage TEXT NOT NULL DEFAULT 'unverified'"
                )
                self._db.execute("ALTER TABLE captures ADD COLUMN timeline_url TEXT")
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN observed_event_ids TEXT NOT NULL DEFAULT '[]'"
                )
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN manifest_complete INTEGER NOT NULL DEFAULT 1"
                )
                self._db.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
        elif version == 2:
            with self._db:
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN observed_event_ids TEXT NOT NULL DEFAULT '[]'"
                )
                self._db.execute(
                    "ALTER TABLE captures ADD COLUMN manifest_complete INTEGER NOT NULL DEFAULT 1"
                )
                self._db.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        if binding_id:
            row = self._db.execute(
                "SELECT value FROM metadata WHERE key='database_binding'"
            ).fetchone()
            if row and row[0] != binding_id:
                raise RuntimeError("Recording ledger belongs to a different Phoenix database")
            if not row:
                with self._db:
                    self._db.execute(
                        "INSERT INTO metadata(key, value) VALUES ('database_binding', ?)",
                        (binding_id,),
                    )
        active = list(
            self._db.execute(
                "SELECT capture_id, gaps FROM captures WHERE state IN ('recording', 'draining')"
            )
        )
        with self._db:
            restarted_at_ns = time_ns()
            for capture_id, gaps_json in active:
                gaps = set(json.loads(gaps_json))
                gaps.add("receiver_restart")
                self._db.execute(
                    "UPDATE captures SET state='incomplete', stopped_at_ns=?, "
                    "drain_deadline_ns=?, "
                    "gaps=? WHERE capture_id=?",
                    (
                        restarted_at_ns,
                        restarted_at_ns,
                        json.dumps(sorted(gaps)),
                        capture_id,
                    ),
                )

    def load(self) -> tuple[dict[str, _Capture], dict[tuple[ConversationKey, str], _Capture]]:
        captures: dict[str, _Capture] = {}
        commands: dict[tuple[ConversationKey, str], _Capture] = {}
        for row in self._db.execute(
            "SELECT capture_id, source, conversation_id, task, request_id, state, "
            "started_at_ns, stopped_at_ns, idle_lease_deadline_ns, hard_deadline_ns, "
            "drain_deadline_ns, persisted_spans, storage, timeline_url, observed_event_ids, "
            "manifest_complete, gaps FROM captures"
        ):
            (capture_id, source, conversation_id, task, request_id, state, started, stopped,
             idle_deadline, hard_deadline, drain_deadline, persisted, storage, timeline_url,
             observed_event_ids, manifest_complete, gaps) = row
            owner = ConversationKey(source, conversation_id)
            capture = _Capture(
                owner=owner,
                task=task,
                request_id=request_id,
                capture_id=capture_id,
                started_at_ns=started,
                stopped_at_ns=stopped,
                idle_lease_deadline_ns=idle_deadline,
                hard_deadline_ns=hard_deadline,
                drain_deadline_ns=drain_deadline,
                persisted_spans=persisted,
                state=state,
                gaps=set(json.loads(gaps)),
                storage=storage,
                timeline_url=timeline_url,
                observed_event_ids=set(json.loads(observed_event_ids)),
                manifest_complete=bool(manifest_complete),
            )
            capture.admitted = {
                (trace_id, span_id)
                for trace_id, span_id in self._db.execute(
                    "SELECT trace_id, span_id FROM span_keys WHERE capture_id=?", (capture_id,)
                )
            }
            captures[capture_id] = capture
            commands[owner, request_id] = capture
        return captures, commands

    def save_capture(self, capture: _Capture) -> None:
        with self._db:
            self._save_capture(capture)

    def save_span(self, capture: _Capture, span_key: tuple[bytes, bytes]) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO span_keys(capture_id, trace_id, span_id) VALUES (?, ?, ?)",
                (capture.capture_id, *span_key),
            )

    def save_admissions(
        self, captures: Iterable[_Capture], span_keys: dict[str, set[tuple[bytes, bytes]]]
    ) -> None:
        """Durably commit one OTLP request's identities and receipts atomically."""
        with self._db:
            for capture in captures:
                self._save_capture(capture)
                self._db.executemany(
                    "INSERT OR IGNORE INTO span_keys(capture_id, trace_id, span_id) "
                    "VALUES (?, ?, ?)",
                    ((capture.capture_id, *key) for key in span_keys.get(capture.capture_id, ())),
                )

    def _save_capture(self, capture: _Capture) -> None:
        self._db.execute(
            "INSERT INTO captures(capture_id, source, conversation_id, task, request_id, "
            "state, started_at_ns, stopped_at_ns, idle_lease_deadline_ns, hard_deadline_ns, "
            "drain_deadline_ns, persisted_spans, storage, timeline_url, observed_event_ids, "
            "manifest_complete, gaps) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(capture_id) DO UPDATE SET state=excluded.state, "
            "stopped_at_ns=excluded.stopped_at_ns, "
            "idle_lease_deadline_ns=excluded.idle_lease_deadline_ns, "
            "hard_deadline_ns=excluded.hard_deadline_ns, "
            "drain_deadline_ns=excluded.drain_deadline_ns, "
            "persisted_spans=excluded.persisted_spans, storage=excluded.storage, "
            "timeline_url=excluded.timeline_url, "
            "observed_event_ids=excluded.observed_event_ids, "
            "manifest_complete=excluded.manifest_complete, gaps=excluded.gaps",
            (
                capture.capture_id, capture.owner.source, capture.owner.conversation_id,
                capture.task, capture.request_id, capture.state, capture.started_at_ns,
                capture.stopped_at_ns, capture.idle_lease_deadline_ns, capture.hard_deadline_ns,
                capture.drain_deadline_ns, capture.persisted_spans, capture.storage,
                capture.timeline_url, json.dumps(sorted(capture.observed_event_ids)),
                int(capture.manifest_complete), json.dumps(sorted(capture.gaps)),
            ),
        )

    def close(self) -> None:
        if hasattr(self, "_db"):
            self._db.close()
            del self._db
        if not self._lock_file.closed:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()


def _strings(attributes: Iterable[KeyValue]) -> dict[str, str]:
    return {
        item.key: item.value.string_value
        for item in attributes
        if item.value.WhichOneof("value") == "string_value"
    }


def managed_source(resource: Iterable[KeyValue], scope_name: str, span: Span) -> Optional[Source]:
    attrs = _strings(resource)
    span_attrs = _strings(span.attributes)
    service = attrs.get("service.name", "").lower()
    if service in ("codex", "codex_cli", "codex_cli_rs", "codex-tui") or scope_name.startswith(
        "codex"
    ):
        return "codex"
    if (
        service == "kiro"
        or attrs.get("openinference.project.name") == "kiro-local"
        or any(key.startswith("kiro.") for key in span_attrs)
    ):
        return "kiro"
    return None


class RecordingGate:
    """One event-loop owner; no raw spans or unbounded history are retained.

    Ancestor evidence is confined to the current bounded request. A late ancestor
    cannot retroactively prove the ownership of an already discarded child.
    """

    def __init__(
        self,
        *,
        max_batch_spans: int = 2048,
        max_captures: int = 128,
        max_capture_spans: int = 10000,
        max_ancestor_depth: int = 128,
        max_request_bytes: int = 8 * 1024 * 1024,
        ledger_path: Optional[Path] = None,
        drain_grace_sec: float = 5.0,
        idle_lease_sec: float = 30 * 60,
        hard_limit_sec: float = 2 * 60 * 60,
        clock_ns: Callable[[], int] = time_ns,
        timeline_origin: str = "http://127.0.0.1:6006",
        database_binding: Optional[str] = None,
    ) -> None:
        if (
            min(
                max_batch_spans,
                max_captures,
                max_capture_spans,
                max_ancestor_depth,
                max_request_bytes,
            )
            < 1
        ):
            raise ValueError("Recording limits must be positive")
        if min(drain_grace_sec, idle_lease_sec, hard_limit_sec) <= 0:
            raise ValueError("Recording lifecycle deadlines must be positive")
        self.max_batch_spans = max_batch_spans
        self.max_captures = max_captures
        self.max_capture_spans = max_capture_spans
        self.max_ancestor_depth = max_ancestor_depth
        self.max_request_bytes = max_request_bytes
        self.drain_grace_ns = int(drain_grace_sec * 1_000_000_000)
        self.idle_lease_ns = int(idle_lease_sec * 1_000_000_000)
        self.hard_limit_ns = int(hard_limit_sec * 1_000_000_000)
        self._clock_ns = clock_ns
        origin = urlsplit(timeline_origin)
        if (
            origin.scheme not in ("http", "https")
            or origin.hostname not in ("127.0.0.1", "localhost", "::1")
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Recording Timeline origin must be loopback HTTP(S)")
        self._timeline_origin = timeline_origin.rstrip("/")
        self._ledger = _RecordingLedger(ledger_path, database_binding) if ledger_path else None
        self._captures, self._commands = (
            self._ledger.load() if self._ledger else ({}, {})
        )
        self._active: dict[ConversationKey, _Capture] = {}
        self._readback_attempted = {
            capture.capture_id
            for capture in self._captures.values()
            if capture.state in ("observed", "empty", "failed")
            or capture.state == "incomplete"
            and (
                "receiver_restart" not in capture.gaps
                or "storage_readback_failed" in capture.gaps
            )
        }
        self.receiver_gaps: dict[str, int] = {}

    def start(self, owner: ConversationKey, task: str, request_id: str) -> CaptureReceipt:
        if not task.strip() or len(task) > 200:
            raise ValueError("Task name must contain 1 to 200 characters")
        UUID(request_id)
        if previous := self._commands.get((owner, request_id)):
            if previous.task != task:
                raise ValueError("Request ID was already used for another task")
            return previous.receipt()
        if owner in self._active:
            raise ValueError("This conversation already has an active recording")
        if len(self._captures) >= self.max_captures:
            raise ValueError("Recording receipt capacity reached")
        capture = _Capture(owner, task, request_id)
        capture.started_at_ns = self._clock_ns()
        capture.hard_deadline_ns = capture.started_at_ns + self.hard_limit_ns
        capture.idle_lease_deadline_ns = min(
            capture.started_at_ns + self.idle_lease_ns, capture.hard_deadline_ns
        )
        # The durable receipt is the admission fence. Never expose the capture
        # to this receiver until persistence succeeds.
        if self._ledger:
            self._ledger.save_capture(capture)
        self._captures[capture.capture_id] = capture
        self._commands[owner, request_id] = capture
        self._active[owner] = capture
        return capture.receipt()

    def status(
        self, owner: ConversationKey, capture_id: Optional[str] = None
    ) -> Optional[CaptureReceipt]:
        capture = self._captures[capture_id] if capture_id else self._active.get(owner)
        if capture is None:
            return None
        if capture.owner != owner:
            raise KeyError("Recording does not belong to this conversation")
        return capture.receipt()

    def stop(
        self,
        owner: ConversationKey,
        capture_id: str,
        observed_event_ids: Iterable[str] = (),
        *,
        manifest_complete: bool = True,
    ) -> CaptureReceipt:
        capture = self._captures[capture_id]
        if capture.owner != owner:
            raise KeyError("Recording does not belong to this conversation")
        if capture.state == "recording":
            event_ids = tuple(observed_event_ids)
            if len(event_ids) > 10_000 or any(
                not isinstance(event_id, str) or not event_id or len(event_id) > 64
                for event_id in event_ids
            ):
                raise ValueError("Kiro stop manifest exceeds its limit")
            if owner.source != "kiro" and event_ids:
                raise ValueError("Only Kiro stop manifests accept event IDs")
            capture.observed_event_ids = set(event_ids)
            capture.manifest_complete = manifest_complete
            if not manifest_complete:
                capture.gaps.add("kiro_manifest_incomplete")
            capture.stopped_at_ns = self._clock_ns()
            capture.drain_deadline_ns = capture.stopped_at_ns + self.drain_grace_ns
            capture.state = "draining"
            if self._ledger:
                self._ledger.save_capture(capture)
        return capture.receipt()

    def tick(self, *, now_ns: Optional[int] = None) -> tuple[CaptureReceipt, ...]:
        """Expire leases and finalize captures after their bounded drain period."""
        now = self._clock_ns() if now_ns is None else now_ns
        finalized: list[CaptureReceipt] = []
        for capture in self._captures.values():
            if capture.state == "recording":
                reason = (
                    "hard_limit_expired"
                    if now >= capture.hard_deadline_ns
                    else "idle_lease_expired"
                    if now >= capture.idle_lease_deadline_ns
                    else None
                )
                if reason:
                    capture.gaps.add(reason)
                    capture.stopped_at_ns = now
                    capture.drain_deadline_ns = now + self.drain_grace_ns
                    capture.state = "draining"
                    if self._ledger:
                        self._ledger.save_capture(capture)
            if (
                capture.state == "draining"
                and capture.drain_deadline_ns is not None
                and now >= capture.drain_deadline_ns
            ):
                capture.state = "incomplete" if capture.admitted else "empty"
                if capture.admitted:
                    capture.gaps.add("storage_unverified")
                self._active.pop(capture.owner, None)
                if self._ledger:
                    self._ledger.save_capture(capture)
                finalized.append(capture.receipt())
        return tuple(finalized)

    def draining_captures_due(self, *, now_ns: Optional[int] = None) -> tuple[CaptureReceipt, ...]:
        now = self._clock_ns() if now_ns is None else now_ns
        return tuple(
            capture.receipt()
            for capture in self._captures.values()
            if (
                capture.drain_deadline_ns is not None
                and capture.drain_deadline_ns <= now
                and (
                    capture.state == "draining"
                    or capture.state == "incomplete"
                    and capture.storage == "unverified"
                    and capture.capture_id not in self._readback_attempted
                )
            )
        )

    def expected_span_keys(self, capture_id: str) -> tuple[tuple[str, str], ...]:
        capture = self._captures[capture_id]
        return tuple(
            sorted(
                (
                    uuid5(
                        NAMESPACE_URL,
                        f"phoenix-recording/{capture.capture_id}/{trace_id.hex()}",
                    ).hex,
                    span_id.hex(),
                )
                for trace_id, span_id in capture.admitted
            )
        )

    def begin_readback(self, capture_id: str) -> bool:
        """Claim the single bounded primary-DB verification attempt for a capture."""
        capture = self._captures[capture_id]
        if capture_id in self._readback_attempted or capture.state not in (
            "draining",
            "incomplete",
        ):
            return False
        self._readback_attempted.add(capture_id)
        return True

    def fail_readback(self, capture_id: str) -> CaptureReceipt:
        capture = self._captures[capture_id]
        capture.state = "incomplete"
        capture.storage = "unverified"
        capture.timeline_url = None
        capture.gaps.add("storage_readback_failed")
        self._active.pop(capture.owner, None)
        if self._ledger:
            self._ledger.save_capture(capture)
        return capture.receipt()

    def complete_drain(self, capture_id: str, result: object) -> CaptureReceipt:
        """Apply exact primary database readback results to a draining capture."""
        capture = self._captures[capture_id]
        if capture.state not in ("draining", "incomplete", "empty"):
            return capture.receipt()
        persisted = set(getattr(result, "persisted_span_keys"))
        expected = set(self.expected_span_keys(capture_id))
        capture.persisted_spans = len(persisted)
        capture.storage = getattr(result, "storage")
        capture.gaps.discard("storage_unverified")
        capture.gaps.update(getattr(result, "gaps", ()))
        if capture.storage == "unverified":
            capture.gaps.add("storage_unverified")
        elif persisted != expected:
            capture.gaps.add("missing_persisted_spans")
        project_id = getattr(result, "project_relay_id", None)
        session_id = getattr(result, "session_relay_id", None)
        if (
            capture.storage in ("partial", "verified")
            and project_id
            and session_id
        ):
            capture.timeline_url = (
                f"{self._timeline_origin}/projects/{quote(project_id, safe='')}"
                f"/sessions/{quote(session_id, safe='')}?sessionView=timeline"
            )
        else:
            capture.timeline_url = None
        if not expected:
            capture.state = "empty"
        elif expected == persisted and not capture.gaps and capture.storage == "verified":
            capture.state = "observed"
        else:
            capture.state = "incomplete"
        self._active.pop(capture.owner, None)
        if self._ledger:
            self._ledger.save_capture(capture)
        return capture.receipt()

    def _gap(self, source: Source, reason: str) -> None:
        key = f"{source}:{reason}"
        self.receiver_gaps[key] = self.receiver_gaps.get(key, 0) + 1

    def admit(self, request: ExportTraceServiceRequest) -> ExportTraceServiceRequest:
        observations = []
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    observations.append(
                        (
                            resource,
                            scope,
                            span,
                            managed_source(resource.resource.attributes, scope.scope.name, span),
                        )
                    )
        over_limit = len(observations) > self.max_batch_spans
        evidence: dict[tuple[Source, bytes, bytes], tuple[Span, Optional[str], Optional[str]]] = {}
        conflicts: set[tuple[Source, bytes, bytes]] = set()
        if not over_limit:
            for _, _, span, source in observations:
                if source is None:
                    continue
                values = [
                    a.value.string_value
                    for a in span.attributes
                    if a.key == ("conversation.id" if source == "codex" else "session.id")
                ]
                identity = values[0] if values else None
                capture_ids = [
                    a.value.string_value
                    for a in span.attributes
                    if a.key == "recording.capture_id"
                ]
                capture_id = capture_ids[0] if capture_ids else None
                key = source, span.trace_id, span.span_id
                if (
                    len(set(values)) > 1
                    or len(set(capture_ids)) > 1
                    or (key in evidence and evidence[key][0] != span)
                ):
                    conflicts.add(key)
                evidence[key] = span, identity, capture_id

        def owner_of(
            source: Source, span: Span
        ) -> Optional[tuple[ConversationKey, Optional[str]]]:
            owners = set()
            capture_ids = set()
            seen = set()
            current = span
            for _ in range(self.max_ancestor_depth):
                key = source, current.trace_id, current.span_id
                if key in seen or key in conflicts:
                    return None
                seen.add(key)
                _, identity, capture_id = evidence[key]
                if identity:
                    try:
                        owners.add(ConversationKey(source, identity))
                    except ValueError:
                        return None
                if capture_id:
                    capture_ids.add(capture_id)
                    if len(capture_ids) > 1:
                        return None
                parent_key = source, current.trace_id, current.parent_span_id
                if not current.parent_span_id or parent_key not in evidence:
                    return (
                        (next(iter(owners)), next(iter(capture_ids), None))
                        if len(owners) == 1
                        else None
                    )
                current = evidence[parent_key][0]
            return None

        output = ExportTraceServiceRequest()
        dirty_captures: dict[str, _Capture] = {}
        admitted_keys: dict[str, set[tuple[bytes, bytes]]] = {}
        for resource in request.resource_spans:
            target_resource = output.resource_spans.add()
            target_resource.CopyFrom(resource)
            for scope in target_resource.scope_spans:
                kept = []
                for span in scope.spans:
                    source = managed_source(resource.resource.attributes, scope.scope.name, span)
                    if source is None:
                        kept.append(span)
                        continue
                    if over_limit:
                        self._gap(source, "batch_limit")
                        continue
                    owner_evidence = owner_of(source, span)
                    if owner_evidence is None:
                        self._gap(source, "unknown_or_conflicting_owner")
                        continue
                    owner, source_capture_id = owner_evidence
                    capture = self._active.get(owner)
                    if capture is None:
                        continue
                    if source == "kiro" and source_capture_id != capture.capture_id:
                        self._gap(source, "capture_mismatch")
                        continue
                    if span.end_time_unix_nano < capture.started_at_ns:
                        capture.gaps.add("completed_before_capture")
                        continue
                    if source == "kiro":
                        span_attributes = _strings(span.attributes)
                        if span_attributes.get("kiro.result") == "incomplete":
                            capture.gaps.add("kiro_incomplete_operation")
                        if (
                            span_attributes.get("kiro.result") == "ambiguous"
                            or span_attributes.get("kiro.record_kind")
                            == "event observation; no operation duration"
                        ):
                            capture.gaps.add("kiro_unpaired_observation")
                    span_key = span.trace_id, span.span_id
                    if span_key in capture.admitted:
                        kept.append(self._map_span(span, capture))
                        continue
                    if capture.state == "draining":
                        event_ids = self._span_event_ids(span)
                        if (
                            source == "kiro"
                            and event_ids
                            and event_ids <= capture.observed_event_ids
                        ):
                            capture.admitted.add(span_key)
                            dirty_captures[capture.capture_id] = capture
                            admitted_keys.setdefault(capture.capture_id, set()).add(span_key)
                            kept.append(self._map_span(span, capture))
                        else:
                            capture.gaps.add("late_unobserved_operation")
                            dirty_captures[capture.capture_id] = capture
                        continue
                    if any(
                        span_key in previous.admitted
                        for previous in self._captures.values()
                        if previous.owner == owner
                    ):
                        continue
                    if len(capture.admitted) >= self.max_capture_spans:
                        capture.gaps.add("capture_limit")
                        capture.stopped_at_ns = self._clock_ns()
                        capture.drain_deadline_ns = (
                            capture.stopped_at_ns + self.drain_grace_ns
                        )
                        capture.state = "draining"
                        dirty_captures[capture.capture_id] = capture
                        continue
                    capture.admitted.add(span_key)
                    dirty_captures[capture.capture_id] = capture
                    admitted_keys.setdefault(capture.capture_id, set()).add(span_key)
                    capture.idle_lease_deadline_ns = min(
                        self._clock_ns() + self.idle_lease_ns, capture.hard_deadline_ns
                    )
                    kept.append(self._map_span(span, capture))
                del scope.spans[:]
                scope.spans.extend(kept)
        if self._ledger and dirty_captures:
            # Persist capture state and admitted identities before returning spans
            # to the gRPC receiver for enqueueing.
            self._ledger.save_admissions(dirty_captures.values(), admitted_keys)
        return output

    def close(self) -> None:
        if self._ledger:
            self._ledger.close()
            self._ledger = None

    @staticmethod
    def _span_event_ids(span: Span) -> set[str]:
        event_ids: set[str] = set()
        for attribute in span.attributes:
            if (
                attribute.key in ("kiro.event_id", "kiro.source_event_ids")
                and attribute.value.WhichOneof("value") == "string_value"
            ):
                event_ids.update(filter(None, attribute.value.string_value.split(",")))
        return event_ids

    @staticmethod
    def _map_span(span: Span, capture: _Capture) -> Span:
        mapped = Span()
        mapped.CopyFrom(span)
        original = _strings(span.attributes)
        provenance = {
            "session.id": capture.capture_id,
            "recording.capture_id": capture.capture_id,
            "recording.source": capture.owner.source,
            "recording.conversation_id": capture.owner.conversation_id,
            "recording.task": capture.task,
            "recording.started_at_ns": str(capture.started_at_ns),
            "recording.original_trace_id": span.trace_id.hex(),
            "recording.original_span_id": span.span_id.hex(),
            "recording.original_parent_span_id": span.parent_span_id.hex(),
            "recording.original_session_id": original.get("session.id", ""),
            "recording.id_mapping": "uuid5-capture-trace-v1",
            "recording.boundary": (
                "crosses_start"
                if span.start_time_unix_nano < capture.started_at_ns
                else "inside_capture"
            ),
        }
        kept = [a for a in mapped.attributes if a.key not in provenance]
        del mapped.attributes[:]
        mapped.attributes.extend(kept)
        for key, value in provenance.items():
            mapped.attributes.add(key=key).value.string_value = value
        mapped.trace_id = uuid5(
            NAMESPACE_URL, f"phoenix-recording/{capture.capture_id}/{span.trace_id.hex()}"
        ).bytes
        return mapped
