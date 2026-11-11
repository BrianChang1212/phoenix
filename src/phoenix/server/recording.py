"""Bounded, process-local admission for explicitly recorded conversations."""

from dataclasses import dataclass, field
from time import time_ns
from typing import Iterable, Literal, Optional
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
    state: Literal["recording", "incomplete", "empty", "failed"]
    started_at_ns: int
    stopped_at_ns: Optional[int]
    queued_spans: int
    gaps: tuple[str, ...]
    session_id: str
    storage: Literal["unverified"] = "unverified"
    project_name: str = RECORDING_PROJECT


@dataclass
class _Capture:
    owner: ConversationKey
    task: str
    request_id: str
    capture_id: str = field(default_factory=lambda: str(uuid4()))
    started_at_ns: int = field(default_factory=time_ns)
    stopped_at_ns: Optional[int] = None
    state: Literal["recording", "incomplete", "empty", "failed"] = "recording"
    admitted: set[tuple[bytes, bytes]] = field(default_factory=set)
    gaps: set[str] = field(default_factory=set)

    def receipt(self) -> CaptureReceipt:
        return CaptureReceipt(
            capture_id=self.capture_id,
            source=self.owner.source,
            conversation_id=self.owner.conversation_id,
            task=self.task,
            state=self.state,
            started_at_ns=self.started_at_ns,
            stopped_at_ns=self.stopped_at_ns,
            queued_spans=len(self.admitted),
            gaps=tuple(sorted(self.gaps)),
            session_id=self.capture_id,
        )


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
        self.max_batch_spans = max_batch_spans
        self.max_captures = max_captures
        self.max_capture_spans = max_capture_spans
        self.max_ancestor_depth = max_ancestor_depth
        self.max_request_bytes = max_request_bytes
        self._captures: dict[str, _Capture] = {}
        self._commands: dict[tuple[ConversationKey, str], _Capture] = {}
        self._active: dict[ConversationKey, _Capture] = {}
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

    def stop(self, owner: ConversationKey, capture_id: str) -> CaptureReceipt:
        capture = self._captures[capture_id]
        if capture.owner != owner:
            raise KeyError("Recording does not belong to this conversation")
        if capture.state == "recording":
            capture.stopped_at_ns = time_ns()
            capture.state = "incomplete" if capture.admitted else "empty"
            if capture.admitted:
                capture.gaps.add("storage_unverified")
            self._active.pop(owner, None)
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
                    span_key = span.trace_id, span.span_id
                    if any(
                        span_key in previous.admitted
                        for previous in self._captures.values()
                        if previous.owner == owner
                    ):
                        continue
                    if len(capture.admitted) >= self.max_capture_spans:
                        capture.gaps.add("capture_limit")
                        capture.state = "failed"
                        capture.stopped_at_ns = time_ns()
                        self._active.pop(owner)
                        continue
                    capture.admitted.add(span_key)
                    kept.append(self._map_span(span, capture))
                del scope.spans[:]
                scope.spans.extend(kept)
        return output

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
