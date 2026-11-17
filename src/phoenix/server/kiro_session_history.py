"""Read an explicit Kiro transcript snapshot and build a historical import.

This module never submits traces. The dedicated import route owns admission;
ordinary managed OTLP traffic must continue through RecordingGate.
"""

import hashlib
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

PROJECT = "kiro-session-history"
IMPORT_SCHEMA_VERSION = 5
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 50_000
MAX_ROW_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_SPANS = 10_000
MAX_REQUEST_BYTES = 8 * 1024 * 1024
VISIBLE_TYPES = frozenset({
    "user", "assistant", "tool_call", "tool_result", "pending_interaction",
    "interaction_resolved", "ContextualHookInvoked", "turn_start", "turn_end",
    "usage_summary", "session_event",
})
EXCLUDED_TYPES = frozenset({
    "agent_note", "session_start", "steering_inclusion", "session_metadata",
})
SUB_AGENT_TYPES = frozenset({"sub_agent_start", "sub_agent_complete"})
SUB_SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class SourceLimitError(ValueError):
    """A Kiro source exceeds its byte, row, or size limit."""


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    title: str
    sha256: str
    records: tuple[dict[str, Any], ...]
    gaps: tuple[str, ...]
    row_count: int
    through_record_id: str
    # (subSessionId, parsed records of its sub-executions file), sorted by id. Rows that
    # are malformed, undatable, or later than a turn boundary are not included.
    sub_runs: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = ()


@dataclass(frozen=True)
class SessionImport:
    import_id: str
    request: ExportTraceServiceRequest
    expected_span_keys: frozenset[tuple[str, str]]
    coverage: dict[str, Any]
    gaps: tuple[str, ...]
    through_record_id: str


def _regular_path(path: Path, root: Path, *, directory: bool = False) -> None:
    if not path.resolve().is_relative_to(root):
        raise ValueError("Kiro source escapes its session store")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Kiro source links and reparse points are not accepted")
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("Kiro source must be a regular file or directory")


def _read_bounded(path: Path, root: Path, limit: int) -> bytes:
    _regular_path(path, root)
    before = path.stat()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Kiro source changed while opening")
        raw = stream.read(limit + 1)
    _regular_path(path, root)
    after = path.stat()
    if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("Kiro source changed while reading")
    if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Kiro source changed while taking its snapshot")
    if len(raw) > limit:
        raise SourceLimitError("Kiro source exceeds its byte limit")
    return raw


def _timestamp(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("Missing source timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source timestamp must have a timezone")
    return int(parsed.timestamp() * 1_000_000_000)


def load_snapshot(
    session_id: str,
    *,
    sessions_root: Optional[Path] = None,
    through_turn_id: Optional[str] = None,
) -> SessionSnapshot:
    if not re.fullmatch(r"sess_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session_id):
        raise ValueError("Invalid Kiro session identity")
    root = (sessions_root or Path.home() / ".kiro/sessions").resolve()
    matches = []
    buckets = list(root.iterdir())
    if len(buckets) > 256:
        raise ValueError("Kiro session store has too many buckets")
    for bucket in buckets:
        if not re.fullmatch(r"[0-9a-f]{16}", bucket.name):
            continue
        directory = bucket / session_id
        if not directory.exists():
            continue
        _regular_path(directory, root, directory=True)
        metadata_raw = _read_bounded(directory / "session.json", root, 65536)
        metadata = json.loads(metadata_raw)
        if not isinstance(metadata, dict) or metadata.get("id") != session_id:
            raise ValueError("Kiro session metadata does not match its directory")
        matches.append((directory / "messages.jsonl", metadata))
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching Kiro Session")
    transcript, metadata = matches[0]
    raw = _read_bounded(transcript, root, MAX_BYTES)
    lines = raw.splitlines(keepends=True)
    boundary_ns: Optional[int] = None
    if through_turn_id is not None:
        if not isinstance(through_turn_id, str) or not 0 < len(through_turn_id) <= 200:
            raise ValueError("Invalid Kiro turn boundary identity")
        boundary = None
        for index, line in enumerate(lines):
            if len(line) > MAX_ROW_BYTES:
                raise ValueError("Kiro transcript row exceeds its byte limit")
            if not line.endswith(b"\n"):
                continue
            try:
                candidate = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = candidate.get("payload") if isinstance(candidate, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("type") == "turn_end"
                and payload.get("boundarySource") == "observed"
                and (payload.get("turnId") or payload.get("executionId")) == through_turn_id
            ):
                boundary = index + 1
                try:
                    boundary_ns = _timestamp(candidate.get("timestamp"))
                except (ValueError, OverflowError, OSError):
                    raise ValueError("Kiro turn boundary has no valid timestamp") from None
                break
        if boundary is None:
            raise ValueError("Requested Kiro turn has no observed completion boundary")
        lines = lines[:boundary]
        raw = b"".join(lines)
    if len(lines) > MAX_ROWS:
        raise SourceLimitError("Kiro transcript exceeds its row limit")
    gaps: set[str] = set()
    records = _parse_rows(lines, gaps)
    if not records:
        raise ValueError("Kiro transcript has no valid timestamped records")
    digest = hashlib.sha256(raw)
    row_count = len(lines)
    sub_runs = []
    sub_ids = sorted({
        r["payload"]["subSessionId"] for r in records
        if r["payload"].get("type") in SUB_AGENT_TYPES and isinstance(r["payload"].get("subSessionId"), str)
    })
    used_bytes = len(raw)
    if used_bytes > MAX_BYTES:
        raise SourceLimitError("Kiro source exceeds its byte limit")
    for sub_id in sub_ids:
        digest.update(b"\nsub-execution:" + sub_id.encode("utf-8") + b"\n")
        sub_raw = None
        if SUB_SESSION_ID.fullmatch(sub_id):
            try:
                sub_raw = _read_bounded(
                    transcript.parent / "sub-executions" / (sub_id + ".jsonl"), root, MAX_BYTES - used_bytes
                )
            except FileNotFoundError:
                sub_raw = None
            except OSError as error:
                raise ValueError("Kiro sub-execution source cannot be read safely") from error
        if sub_raw is None:
            gaps.add("sub_execution_missing")
            digest.update(b"<missing>")
            continue
        used_bytes += len(sub_raw)
        sub_lines = sub_raw.splitlines(keepends=True)
        if boundary_ns is not None:
            sub_lines = _cut_after(sub_lines, boundary_ns)
        digest.update(b"".join(sub_lines))
        row_count += len(sub_lines)
        if row_count > MAX_ROWS:
            raise SourceLimitError("Kiro transcript exceeds its row limit")
        sub_runs.append((sub_id, tuple(_parse_rows(sub_lines, gaps, sub_id))))
    title = metadata.get("title")
    return SessionSnapshot(
        session_id=session_id,
        title=title[:200] if isinstance(title, str) else session_id,
        sha256=digest.hexdigest(),
        records=tuple(records), gaps=tuple(sorted(gaps)), row_count=row_count,
        through_record_id=records[-1]["id"], sub_runs=tuple(sub_runs),
    )


def _row_ns(line: bytes) -> Optional[int]:
    """Source timestamp of a complete JSON row, or None when it has none."""
    if not line.endswith(b"\n"):
        return None
    try:
        record = json.loads(line)
        return _timestamp(record.get("timestamp")) if isinstance(record, dict) else None
    except (ValueError, UnicodeDecodeError, OverflowError, OSError):
        return None


def _cut_after(lines: list[bytes], boundary_ns: int) -> list[bytes]:
    """Drop rows stamped after the boundary, and undatable rows that follow the first such row.

    Sub-execution files are append-only, so the dropped suffix is exactly what a
    later snapshot would add; keeping the rest makes the import independent of it.
    """
    kept = []
    cut = False
    for line in lines:
        ns = _row_ns(line)
        if ns is not None and ns > boundary_ns:
            cut = True
        elif ns is not None or not cut:
            kept.append(line)
    return kept


def _parse_rows(lines: list[bytes], gaps: set[str], sub_id: Optional[str] = None) -> list[dict[str, Any]]:
    records = []
    seen = {}
    for index, line in enumerate(lines, 1):
        if len(line) > MAX_ROW_BYTES:
            raise SourceLimitError("Kiro transcript row exceeds its byte limit")
        if not line.endswith(b"\n"):
            gaps.add("unfinished_row")
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise ValueError("Invalid transcript row")
            ident = record.get("id")
            if not isinstance(ident, str) or not 0 < len(ident) <= 512:
                raise ValueError("Invalid transcript record ID")
            if sub_id is not None and record["payload"].get("subExecutionId", sub_id) != sub_id:
                raise ValueError("Sub-execution row belongs to another run")
        except (ValueError, UnicodeDecodeError):
            gaps.add("malformed_row")
            continue
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if ident in seen:
            if seen[ident] != canonical:
                raise ValueError("Conflicting duplicate Kiro record ID")
            continue
        seen[ident] = canonical
        record["source_line"] = index
        try:
            record["source_ns"] = _timestamp(record.get("timestamp"))
        except (ValueError, OverflowError, OSError):
            gaps.add("invalid_timestamp")
            continue
        records.append(record)
    return records


def _redact_structure(value: object) -> object:
    if isinstance(value, dict):
        sensitive = {"authorization", "proxyauthorization", "password", "passwd", "apikey", "accesstoken", "refreshtoken", "secret", "token", "privatekey"}
        return {
            key: "[redacted]" if re.sub(r"[^a-z]", "", str(key).lower()) in sensitive else _redact_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    return value


def _clean_text(value: object, gaps: set[str]) -> str:
    if not isinstance(value, str):
        value = json.dumps(_redact_structure(value), ensure_ascii=False, sort_keys=True)
    elif value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
            redacted = _redact_structure(decoded)
            if redacted != decoded:
                value = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        except ValueError:
            pass
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted private key]", value, flags=re.S)
    value = re.sub(r"(?im)^\s*(?:proxy-)?authorization\s*:[^\r\n]*", "Authorization: [redacted]", value)
    value = re.sub(r"(?i)\b(password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1=[redacted]", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b", "[redacted]", value)
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    raw = value.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        gaps.add("content_truncated")
        return raw[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"
    return value


def _sub_agent_windows(
    subs: dict[str, dict[str, Any]], steps: list[dict[str, Any]], gaps: set[str]
) -> dict[str, tuple[int, int]]:
    """Window of each sub-agent span: its start/complete records widened to hold every child step."""
    child_times: dict[str, list[int]] = {}
    for step in steps:
        if step["run"] is not None:
            child_times.setdefault(step["run"], []).append(step["record"]["source_ns"])
    windows = {}
    for sub_id, entry in subs.items():
        opened = entry.get("start") or entry["complete"]
        closed = entry.get("complete") or opened
        begin, finish = opened["source_ns"], closed["source_ns"]
        if finish < begin:
            gaps.add("invalid_sub_agent_interval")
            finish = begin
        children = child_times.get(sub_id, [])
        windows[sub_id] = (min([begin, *children]), max([finish, *children]))
    return windows


def _turn_windows(
    turns: dict[str, dict[str, Any]],
    subs: dict[str, dict[str, Any]],
    sub_windows: dict[str, tuple[int, int]],
    steps: list[dict[str, Any]],
    gaps: set[str],
) -> dict[str, tuple[int, int, bool]]:
    """Window of each turn span (start, end, observed), widened to hold its steps and sub-agent spans."""
    step_times: dict[str, list[int]] = {}
    for step in steps:
        if step["run"] is None and step["turn_id"] in turns:
            step_times.setdefault(step["turn_id"], []).append(step["record"]["source_ns"])
    windows = {}
    for turn_id, boundary in turns.items():
        start, end = boundary["start"], boundary["end"]
        if end is None:
            gaps.add("open_turn")
        observed = start["payload"].get("boundarySource") == "observed" and end is not None and end["payload"].get("boundarySource") == "observed"
        if not observed:
            gaps.add("unverified_turn_boundary")
        begin, finish = start["source_ns"], end["source_ns"] if observed else start["source_ns"]
        if finish < begin:
            gaps.add("invalid_turn_interval")
            finish = begin
        children = step_times.get(turn_id, [])
        begin, finish = min([begin, *children]), max([finish, *children])
        for sub_id, entry in subs.items():
            if entry.get("turn_id") == turn_id:
                begin, finish = min(begin, sub_windows[sub_id][0]), max(finish, sub_windows[sub_id][1])
        windows[turn_id] = (begin, finish, observed)
    return windows


def _sub_agent_attributes(sub_id: str, entry: dict[str, Any], gaps: set[str]) -> tuple[dict[str, str], str]:
    """Span attributes and the result label of one sub-agent run."""
    source = [entry[slot] for slot in ("start", "complete") if slot in entry]
    start_record, complete_record = entry.get("start"), entry.get("complete")
    info = (start_record or complete_record)["payload"]
    raw_status = str(complete_record["payload"].get("status", "unknown")) if complete_record else "incomplete"
    result = "success" if raw_status == "success" else "incomplete" if not complete_record else "unknown" if raw_status == "unknown" else "error"
    attributes = {
        "sessionlog.kind": "sub_agent", "sessionlog.sub_session_id": sub_id,
        "sessionlog.source_record_ids": json.dumps([r["id"] for r in source]),
        "sessionlog.source_lines": json.dumps([r["source_line"] for r in source]),
        "sessionlog.source_timestamps": json.dumps([r["timestamp"] for r in source]),
        "sessionlog.timing": "observed sub-agent start and complete records" if complete_record and start_record else "source point; incomplete sub-agent",
        "openinference.span.kind": "AGENT",
        "kiro.sub_agent.name": _clean_text(info.get("subAgentName", "unknown"), gaps),
        "kiro.sub_agent.status": _clean_text(raw_status, gaps),
        "kiro.result": result,
    }
    for key, field, source_payload in (
        ("kiro.sub_agent.explanation", "explanation", info),
        ("input.value", "prompt", info),
        ("output.value", "response", complete_record["payload"] if complete_record else {}),
    ):
        if field in source_payload:
            attributes[key] = _clean_text(source_payload[field], gaps)
    attributes["kiro.action_summary"] = _clean_text("Sub-agent: " + str(info.get("subAgentName", "unknown")), gaps)
    return attributes, result


def build_import(snapshot: SessionSnapshot) -> SessionImport:
    import_id = str(uuid5(NAMESPACE_URL, f"kiro-session-history:v{IMPORT_SCHEMA_VERSION}:" + snapshot.session_id + ":" + snapshot.sha256))
    trace_id = uuid5(NAMESPACE_URL, import_id + ":trace").bytes
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(key="openinference.project.name").value.string_value = PROJECT
    resource.resource.attributes.add(key="service.name").value.string_value = "kiro-session-history-import"
    scope = resource.scope_spans.add()
    scope.scope.name = "phoenix.kiro-session-history"
    gaps = set(snapshot.gaps)
    excluded: Counter[str] = Counter()
    included: Counter[str] = Counter()
    steps = []
    subs: dict[str, dict[str, Any]] = {}
    turns: dict[str, dict[str, Any]] = {}
    tool_steps = 0

    def ingest(records: tuple[dict[str, Any], ...], run: Optional[str] = None) -> None:
        nonlocal tool_steps
        calls: dict[str, dict[str, Any]] = {}
        results: dict[str, dict[str, Any]] = {}
        current_turn = None
        for record in records:
            payload = record["payload"]
            kind = payload.get("type")
            if run is None and kind in SUB_AGENT_TYPES:
                sub_id = payload.get("subSessionId")
                if not isinstance(sub_id, str) or not sub_id:
                    excluded[kind] += 1
                    gaps.add("unpaired_sub_agent")
                    continue
                included[kind] += 1
                entry = subs.setdefault(sub_id, {})
                slot = "start" if kind == "sub_agent_start" else "complete"
                if slot in entry:
                    gaps.add("unpaired_sub_agent")
                    continue
                entry[slot] = record
                parent_id = payload.get("parentExecutionId")
                if slot == "start" or "turn_id" not in entry:
                    entry["turn_id"] = parent_id if parent_id in turns else current_turn
                continue
            if kind in EXCLUDED_TYPES or kind == "assistant" and payload.get("operationType") != "Say":
                excluded[str(kind) + ("." + str(payload.get("operationType")) if kind == "assistant" else "")] += 1
                if kind == "assistant" and payload.get("operationType") != "Reasoning":
                    gaps.add("unsupported_assistant_operation")
                continue
            if kind not in VISIBLE_TYPES:
                excluded[str(kind)] += 1
                gaps.add("unknown_event_type")
                continue
            included[kind] += 1
            turn_id = payload.get("turnId")
            if kind == "turn_start" and run is None:
                turn_id = turn_id or payload.get("executionId")
                if not isinstance(turn_id, str) or not turn_id:
                    gaps.add("missing_turn_id")
                else:
                    if turn_id in turns:
                        gaps.add("duplicate_turn_start")
                    turns[turn_id] = {"start": record, "end": None}
                    current_turn = turn_id
            if not isinstance(turn_id, str) or not turn_id:
                turn_id = current_turn
            step = {"record": record, "records": [record], "kind": kind, "turn_id": turn_id, "run": run}
            if kind in ("tool_call", "tool_result"):
                tool_id = payload.get("toolCallId")
                if not isinstance(tool_id, str) or not tool_id:
                    gaps.add("missing_tool_call_id")
                    steps.append(step)
                    continue
                target = calls if kind == "tool_call" else results
                if tool_id in target:
                    raise ValueError("Ambiguous duplicate toolCallId in transcript snapshot")
                target[tool_id] = step
            else:
                steps.append(step)
            if kind == "turn_end" and run is None:
                if turn_id not in turns:
                    gaps.add("orphan_turn_end")
                else:
                    turns[turn_id]["end"] = record
                if current_turn == turn_id:
                    current_turn = None

        for tool_id in sorted(calls.keys() | results.keys()):
            call, result = calls.get(tool_id), results.get(tool_id)
            step = dict(result or call)
            step["kind"] = "tool"
            step["records"] = ([call["record"]] if call else []) + ([result["record"]] if result else [])
            step["call"] = call["record"]["payload"] if call else None
            step["result"] = result["record"]["payload"] if result else None
            step["tool_id"] = tool_id
            step["turn_id"] = (call or result)["turn_id"]
            if not call:
                gaps.add("orphan_tool_result")
            if not result:
                gaps.add("missing_tool_result")
            steps.append(step)
        tool_steps += len(calls.keys() | results.keys())

    ingest(snapshot.records)
    for sub_id, sub_records in snapshot.sub_runs:
        ingest(sub_records, sub_id)
    for entry in subs.values():
        if "start" not in entry or "complete" not in entry:
            gaps.add("unpaired_sub_agent")

    common = {
        "session.id": import_id, "recording.capture_id": import_id,
        "recording.mode": "session_transcript", "recording.source": "kiro",
        "sessionlog.original_session_id": snapshot.session_id,
        "sessionlog.snapshot_sha256": snapshot.sha256,
    }

    def add_span(key: str, name: str, start: int, end: int, attributes: dict[str, str], parent: bytes = b"", status: str = "unknown") -> bytes:
        span_id = uuid5(NAMESPACE_URL, import_id + ":" + key).bytes[:8]
        span = scope.spans.add(trace_id=trace_id, span_id=span_id, parent_span_id=parent,
                               name=_clean_text(name, gaps), start_time_unix_nano=start, end_time_unix_nano=end)
        for name, value in {**common, **attributes}.items():
            span.attributes.add(key=name).value.string_value = value
        span.status.code = 1 if status == "success" else 2 if status in ("error", "denied", "timeout") else 0
        return span_id

    sub_windows = _sub_agent_windows(subs, steps, gaps)
    turn_windows = _turn_windows(turns, subs, sub_windows, steps, gaps)
    visible_times = [item["record"]["source_ns"] for item in steps]
    for window in (*sub_windows.values(), *(w[:2] for w in turn_windows.values())):
        visible_times.extend(window)
    if not visible_times:
        raise ValueError("Kiro transcript has no supported visible steps")
    root_id = add_span("root", "kiro.session", min(visible_times), max(visible_times), {
        "sessionlog.kind": "session", "kiro.action_summary": _clean_text("匯入 Kiro Session：" + snapshot.title, gaps),
        "sessionlog.timing": "envelope of source timestamps; not measured execution duration",
        "openinference.span.kind": "CHAIN",
    })
    turn_parents = {}
    for turn_id, (begin, finish, observed) in turn_windows.items():
        turn_parents[turn_id] = add_span("turn:" + turn_id, "Kiro turn", begin, finish, {
            "sessionlog.kind": "turn", "sessionlog.turn_id": turn_id,
            "sessionlog.timing": "observed turn boundaries" if observed else "source point; incomplete turn",
            "openinference.span.kind": "CHAIN",
        }, root_id)

    sub_parents: dict[str, bytes] = {}
    for sub_id, entry in sorted(subs.items()):
        attributes, result = _sub_agent_attributes(sub_id, entry, gaps)
        begin, finish = sub_windows[sub_id]
        sub_parents[sub_id] = add_span(
            "subagent:" + json.dumps(sub_id), ("Sub-agent: " + attributes.get("kiro.sub_agent.name", "unknown"))[:80], begin, finish, attributes,
            turn_parents.get(entry.get("turn_id"), root_id), result,
        )

    for ordinal, step in enumerate(sorted(steps, key=lambda s: (s["record"]["source_ns"], s["run"] or "", s["record"]["source_line"]))):
        record, kind = step["record"], step["kind"]
        payload = record["payload"]
        title = kind
        status = "unknown"
        attributes = {
            "sessionlog.kind": kind,
            "sessionlog.source_record_ids": json.dumps([r["id"] for r in step["records"]]),
            "sessionlog.source_lines": json.dumps([r["source_line"] for r in step["records"]]),
            "sessionlog.source_timestamps": json.dumps([r["timestamp"] for r in step["records"]]),
            "sessionlog.source_line": str(record["source_line"]),
            "sessionlog.source_timestamp": record["timestamp"],
            "sessionlog.order": str(ordinal + 1),
            "sessionlog.timing": "source point; execution duration not provided",
            "openinference.span.kind": "CHAIN",
        }
        if kind == "tool":
            call, result = step["call"], step["result"]
            title = (call or {}).get("title") or (call or {}).get("toolName") or "Tool result without call"
            attributes["openinference.span.kind"] = "TOOL"
            attributes["sessionlog.tool_call_id"] = step["tool_id"]
            if call:
                attributes["input.value"] = _clean_text(call.get("args", {}), gaps)
            if result:
                attributes["output.value"] = _clean_text(result.get("content", ""), gaps)
                status = "success" if result.get("success") is True else "error" if result.get("success") is False else "unknown"
                attributes["sessionlog.result_semantics"] = "native tool result; not business outcome"
                if (call or {}).get("toolName") in ("execute_pwsh", "execute_bash"):
                    content = result.get("content")
                    match = re.search(r"(?:^|\n)Exit Code: (-?\d+)\s*$", content) if isinstance(content, str) else None
                    if match:
                        code = int(match[1])
                        attributes["kiro.exit_code"] = str(code)
                        status = "success" if code == 0 else "error"
                        if code == -1 and "Command timed out after " in content:
                            status = "timeout"
                        elif code == -1 and "permission" in content.lower():
                            status = "denied"
                        attributes["sessionlog.result_semantics"] = "shell exit code; not business outcome"
                    else:
                        status = "error" if result.get("success") is False else "unknown"
                        attributes["sessionlog.result_semantics"] = "native tool completion; shell exit code unavailable; not business outcome"
                if not call:
                    status = "incomplete"
            else:
                status = "incomplete"
            attributes["kiro.result"] = status
        elif kind in ("user", "assistant"):
            title = "使用者訊息" if kind == "user" else "助理回覆"
            attributes["input.value" if kind == "user" else "output.value"] = _clean_text(payload.get("content", ""), gaps)
        elif kind == "ContextualHookInvoked":
            title = "Hook: " + str(payload.get("name", "unknown"))
            attributes["sessionlog.source_status"] = str(payload.get("status", "unknown"))
        elif kind == "pending_interaction":
            title = "等待權限／互動：" + str(payload.get("interactionType", "unknown"))
            attributes["input.value"] = _clean_text(payload.get("question", ""), gaps)
        elif kind == "interaction_resolved":
            title = "權限／互動結果：" + str(payload.get("outcome", "unknown"))
        elif kind == "usage_summary":
            title = "Usage summary"
            attributes["sessionlog.source_status"] = str(payload.get("status", "unknown"))
        elif kind == "session_event":
            title = "Session event: " + str(payload.get("category", "unknown"))
        attributes["kiro.action_summary"] = _clean_text(title, gaps)
        source_key = json.dumps([r["id"] for r in step["records"]], separators=(",", ":"))
        if step["run"] is None:
            parent = turn_parents.get(step["turn_id"], root_id)
            key = "step:" + source_key
        else:
            parent = sub_parents[step["run"]]
            key = "substep:" + json.dumps([step["run"], [r["id"] for r in step["records"]]], separators=(",", ":"))
        add_span(key, attributes["kiro.action_summary"], record["source_ns"], record["source_ns"], attributes, parent, status)

    if len(scope.spans) > MAX_SPANS or request.ByteSize() > MAX_REQUEST_BYTES:
        raise ValueError("Kiro Session import exceeds its span or request limit")
    coverage = {
        "import_schema_version": IMPORT_SCHEMA_VERSION,
        "source_rows": snapshot.row_count,
        "valid_records": len(snapshot.records) + sum(len(rows) for _, rows in snapshot.sub_runs),
        "included_by_type": dict(sorted(included.items())),
        "excluded_by_type": dict(sorted(excluded.items())),
        "visible_steps": len(steps),
        "spans": len(scope.spans),
        "tool_steps": tool_steps,
        "sub_agent_runs": len(subs),
        "complete": not gaps,
        "snapshot_sha256": snapshot.sha256,
    }
    expected = frozenset((s.trace_id.hex(), s.span_id.hex()) for s in scope.spans)
    if len(expected) != len(scope.spans):
        raise ValueError("Kiro Session import contains colliding span identities")
    return SessionImport(import_id, request, expected, coverage, tuple(sorted(gaps)), snapshot.through_record_id)
