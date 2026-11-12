"""Local client observations owned by Phoenix, independent of client process jobs."""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

import psutil
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt

if TYPE_CHECKING:
    from phoenix.server.recording import BoundLifetime, RecordingGate

MAX_BASELINE_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_POLL_BYTES = 1024 * 1024
MAX_LIFETIME_NS = 2 * 60 * 60 * 1_000_000_000
logger = logging.getLogger(__name__)
LifetimeReason = Literal[
    "client_exited", "turn_cancelled", "observation_unavailable", "hard_limit_expired"
]


class ProcessIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pid: StrictInt = Field(gt=0)
    created: FiniteFloat = Field(gt=0)
    exe: str = Field(min_length=1, max_length=2048)


class LifetimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner: ProcessIdentity
    transcript: Optional[str] = Field(default=None, max_length=2048)
    file_id: Optional[tuple[StrictInt, StrictInt]] = None
    offset: StrictInt = Field(default=0, ge=0)
    turn_id: Optional[str] = Field(default=None, min_length=1, max_length=200)


def allowed_executable(source: str, executable: Path) -> bool:
    home = Path.home()
    if source == "kiro":
        run_root = (home / "AppData/Local/Kiro-Cli/run").resolve()
        return bool(
            executable.parent == run_root
            and re.fullmatch(r"chat-cli-\d+(?:\.\d+)+(?:[-.][\w]+)*\.exe", executable.name, re.I)
        ) or executable in {
            (home / "AppData/Local/Kiro-Cli/kiro-cli.exe").resolve(),
            (home / ".local/bin/kiro-cli.exe").resolve(),
            (home / ".local/bin/kiro-cli").resolve(),
        }
    if source == "codex" and executable.name.lower() in ("codex.exe", "codex"):
        if executable == (home / ".local/bin" / executable.name).resolve():
            return True
        package = home / "AppData/Roaming/npm/node_modules/@openai/codex"
        if executable.is_relative_to(package.resolve()):
            return True
        extensions = (home / ".vscode/extensions").resolve()
        if executable.is_relative_to(extensions):
            return executable.relative_to(extensions).parts[0].startswith("openai.chatgpt-")
    return False


def process_alive(owner: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(owner.pid)
        return (
            process.is_running()
            and process.create_time() == owner.created
            and os.path.normcase(str(Path(process.exe()).resolve())) == os.path.normcase(owner.exe)
        )
    except psutil.NoSuchProcess:
        return False


def _payload(line: bytes) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return {}
    result = record.get("payload") if isinstance(record, dict) else None
    return result if isinstance(result, dict) else {}


def validate_local_binding(
    source: str, conversation_id: str, binding: LifetimeBinding
) -> LifetimeBinding:
    executable = Path(binding.owner.exe)
    if not executable.is_absolute() or not allowed_executable(source, executable.resolve()):
        raise ValueError("Recording lifetime executable is not an installed local client")
    try:
        live = process_alive(binding.owner)
    except psutil.Error as error:
        raise ValueError("Recording lifetime process identity cannot be inspected") from error
    if not live:
        raise ValueError("Recording lifetime process identity is not live")
    if source == "codex":
        if (
            binding.transcript is not None
            or binding.file_id is not None
            or binding.turn_id is not None
            or binding.offset
        ):
            raise ValueError("Codex lifetime bindings accept only process metadata")
        return binding
    if source != "kiro" or not re.fullmatch(r"sess_[0-9a-f-]{36}", conversation_id):
        raise ValueError("Invalid local recording conversation")
    if binding.transcript is None or binding.file_id is None or binding.turn_id is None:
        raise ValueError("Kiro lifetime requires its active transcript cursor")
    path = Path(binding.transcript)
    root = (Path.home() / ".kiro/sessions").resolve()
    resolved = path.resolve(strict=True)
    if (
        not path.is_absolute()
        or resolved.name != "messages.jsonl"
        or resolved.parent.name != conversation_id
        or resolved.parent.parent.parent != root
        or not re.fullmatch(r"[0-9a-f]{16}", resolved.parent.parent.name)
    ):
        raise ValueError("Kiro lifetime transcript is outside its exact session directory")
    metadata = resolved.parent / "session.json"
    if metadata.resolve(strict=True).parent != resolved.parent:
        raise ValueError("Kiro lifetime metadata escapes its session directory")
    with metadata.open("rb") as stream:
        raw_metadata = stream.read(65537)
    if len(raw_metadata) > 65536:
        raise ValueError("Kiro lifetime session metadata exceeds its limit")
    metadata_value = json.loads(raw_metadata)
    if not isinstance(metadata_value, dict) or metadata_value.get("id") != conversation_id:
        raise ValueError("Kiro lifetime metadata belongs to another session")
    with resolved.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if (stat.st_dev, stat.st_ino) != binding.file_id or binding.offset > stat.st_size:
            raise ValueError("Kiro lifetime transcript identity or offset changed")
        start = max(0, stat.st_size - MAX_BASELINE_BYTES)
        stream.seek(start)
        raw = stream.read(MAX_BASELINE_BYTES)
    if start:
        raw = raw.partition(b"\n")[2]
    if raw and not raw.endswith(b"\n"):
        raise ValueError("Kiro lifetime transcript has an unfinished row; retry START")
    active = None
    for line in raw.splitlines():
        if len(line) > MAX_LINE_BYTES:
            continue
        payload = _payload(line)
        if payload.get("boundarySource") != "observed":
            continue
        ident = payload.get("turnId") or payload.get("executionId")
        if payload.get("type") == "turn_start":
            active = ident
        elif payload.get("type") == "turn_end" and ident == active:
            active = None
    if active != binding.turn_id:
        raise ValueError("Kiro lifetime binding does not identify its active turn")
    return binding


class LifetimeProbe:
    def __init__(self, binding: LifetimeBinding, hard_deadline_ns: int):
        self.binding = binding
        remaining = min(max(0, hard_deadline_ns - time.time_ns()), MAX_LIFETIME_NS)
        self.deadline = time.monotonic() + remaining / 1_000_000_000
        self.hard_deadline_ns = hard_deadline_ns
        self.offset = binding.offset
        self.turn_id = binding.turn_id
        self.pending = b""
        self.discarding = False
        self.reason: Optional[LifetimeReason] = None

    def probe(self) -> Optional[LifetimeReason]:
        if self.reason is not None:
            return self.reason
        if time.monotonic() >= self.deadline or time.time_ns() >= self.hard_deadline_ns:
            self.reason = "hard_limit_expired"
            return self.reason
        try:
            if not process_alive(self.binding.owner):
                self.reason = "client_exited"
            elif self.binding.transcript is not None and self._cancelled():
                self.reason = "turn_cancelled"
        except (OSError, ValueError, psutil.AccessDenied):
            self.reason = "observation_unavailable"
        return self.reason

    def _cancelled(self) -> bool:
        assert self.binding.transcript is not None
        with Path(self.binding.transcript).open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) != self.binding.file_id or stat.st_size < self.offset:
                raise ValueError("Kiro transcript changed identity or was truncated")
            stream.seek(self.offset)
            chunk = stream.read(MAX_POLL_BYTES)
            self.offset = stream.tell()
        found = False
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if not self.discarding:
                if len(self.pending) + len(part) > MAX_LINE_BYTES:
                    self.pending = b""
                    self.discarding = True
                else:
                    self.pending += part
            if not complete:
                continue
            if self.discarding:
                self.discarding = False
                continue
            line, self.pending = self.pending, b""
            payload = _payload(line)
            if payload.get("boundarySource") != "observed":
                continue
            ident = payload.get("turnId") or payload.get("executionId")
            if (
                payload.get("type") == "turn_start"
                and isinstance(ident, str)
                and 0 < len(ident) <= 200
            ):
                self.turn_id = ident
            elif payload.get("type") == "turn_end" and ident == self.turn_id:
                found = found or payload.get("stopReason") == "cancelled"
                self.turn_id = None
        return found


def _probe_snapshot(
    snapshot: tuple["BoundLifetime", ...],
) -> list[tuple["BoundLifetime", LifetimeReason]]:
    results = []
    for item in snapshot:
        try:
            reason = item.probe.probe()
        except Exception:
            logger.exception(
                "Recording lifetime observation failed for capture %s", item.capture_id
            )
            reason = "observation_unavailable"
        if reason:
            item.probe.reason = reason
            results.append((item, reason))
    return results


async def maintain_lifetimes(gate: "RecordingGate", *, poll_seconds: float = 0.5) -> None:
    while True:
        snapshot = gate.active_lifetimes()
        if snapshot:
            observations = await asyncio.to_thread(_probe_snapshot, snapshot)
            for item, reason in observations:
                try:
                    gate.stop_for_lifetime(item, reason)
                except Exception:
                    logger.exception(
                        "Recording lifetime stop failed for capture %s", item.capture_id
                    )
        await asyncio.sleep(poll_seconds)
