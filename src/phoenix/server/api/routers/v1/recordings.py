from dataclasses import asdict
from ipaddress import ip_address
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from phoenix.server.recording import ConversationKey, RecordingGate
from phoenix.server.recording_lifetime import LifetimeBinding, validate_local_binding

router = APIRouter(tags=["recordings"], include_in_schema=False)


class RecordingOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["codex", "kiro"]
    conversation_id: str = Field(min_length=1, max_length=200)


class StartRecording(RecordingOwner):
    task: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=36, max_length=36)
    lifetime: Optional[LifetimeBinding] = None


class SelectRecording(RecordingOwner):
    capture_id: Optional[str] = Field(default=None, min_length=36, max_length=36)


class StopRecording(RecordingOwner):
    capture_id: str = Field(min_length=36, max_length=36)
    observed_event_ids: list[str] = Field(default_factory=list, max_length=10_000)
    manifest_complete: bool = True


class ImportKiroSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^sess_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    task: str = Field(default="Kiro complete Session", min_length=1, max_length=200)
    through_turn_id: Optional[str] = Field(default=None, min_length=1, max_length=200)


class QueueKiroSession(ImportKiroSession):
    through_turn_id: str = Field(min_length=1, max_length=200)


def _gate(request: Request) -> RecordingGate:
    try:
        loopback = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HTTPException(403, "Recording controls require a loopback client")
    if request.headers.get("origin"):
        raise HTTPException(403, "Recording controls do not accept browser origins")
    gate = getattr(request.app.state, "recording_gate", None)
    if not isinstance(gate, RecordingGate):
        raise HTTPException(503, "Recording receiver is not enabled")
    return gate


@router.post("/recordings/start")
async def start_recording(body: StartRecording, request: Request) -> dict[str, Any]:
    gate = _gate(request)
    try:
        lifetime = (
            validate_local_binding(body.source, body.conversation_id, body.lifetime)
            if body.lifetime is not None
            else None
        )
        return asdict(
            gate.start(
                ConversationKey(body.source, body.conversation_id),
                body.task,
                body.request_id,
                lifetime=lifetime,
            )
        )
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/recordings/status")
async def recording_status(body: SelectRecording, request: Request) -> dict[str, Any]:
    gate = _gate(request)
    try:
        receipt = gate.status(ConversationKey(body.source, body.conversation_id), body.capture_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(404, "Recording not found for this conversation") from error
    result = asdict(receipt) if receipt else {"state": "off", **body.model_dump()}
    return {**result, "receiver_gaps": dict(gate.receiver_gaps)}


@router.post("/recordings/stop")
async def stop_recording(body: StopRecording, request: Request) -> dict[str, Any]:
    gate = _gate(request)
    try:
        return asdict(
            gate.stop(
                ConversationKey(body.source, body.conversation_id),
                body.capture_id,
                body.observed_event_ids,
                manifest_complete=body.manifest_complete,
            )
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(404, "Recording not found for this conversation") from error


@router.post("/recordings/kiro-session/import")
async def import_kiro_session(body: ImportKiroSession, request: Request) -> dict[str, Any]:
    """Import an explicitly selected local transcript, separately from live captures."""
    _gate(request)
    from phoenix.server.api.routers.v1.traces import _add_spans
    from phoenix.server.kiro_session_history import PROJECT, build_import, load_snapshot
    from phoenix.server.recording_readback import settle_recording_spans

    try:
        snapshot = await run_in_threadpool(
            load_snapshot, body.session_id,
            **({"through_turn_id": body.through_turn_id} if body.through_turn_id else {}),
        )
        imported = await run_in_threadpool(build_import, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(409, "Cannot import this local Kiro Session snapshot") from error
    # Only server-generated spans from the validated local source enter this path.
    # The public OTLP route continues to enforce the normal live RecordingGate.
    await _add_spans(imported.request, request.state, PROJECT, recording_enabled=False)
    gaps = set(imported.gaps)
    try:
        readback = await settle_recording_spans(
            request.app.state.db, capture_id=imported.import_id,
            expected_span_keys=imported.expected_span_keys, project_name=PROJECT,
        )
    except (OSError, RuntimeError, TimeoutError):
        readback = None
        gaps.add("storage_readback_failed")
    storage = readback.storage if readback is not None else "unverified"
    persisted = len(readback.persisted_span_keys) if readback is not None else 0
    if storage != "verified":
        gaps.add("storage_not_verified")
    timeline_url = None
    if readback is not None and readback.project_relay_id and readback.session_relay_id:
        from urllib.parse import quote

        origin = f"http://127.0.0.1:{request.url.port or 80}"
        timeline_url = (
            f"{origin}/projects/{quote(readback.project_relay_id, safe='')}"
            f"/sessions/{quote(readback.session_relay_id, safe='')}?sessionView=timeline"
        )
    return {
        "mode": "session_transcript", "state": "imported" if not gaps else "incomplete",
        "source_session_id": body.session_id, "import_id": imported.import_id,
        "task": body.task, "source_sha256": snapshot.sha256,
        "through_turn_id": body.through_turn_id,
        "through_record_id": imported.through_record_id,
        "storage": storage, "expected_spans": len(imported.expected_span_keys),
        "persisted_spans": persisted, "gaps": sorted(gaps),
        "coverage": imported.coverage, "timeline_url": timeline_url,
    }


@router.post("/recordings/kiro-session/queue")
async def queue_kiro_session(body: QueueKiroSession, request: Request) -> dict[str, Any]:
    """Let Phoenix own the wait for a final turn, independently of the Kiro client."""
    _gate(request)
    from phoenix.server.kiro_session_queue import KiroSessionQueue

    queue = getattr(request.app.state, "kiro_session_queue", None)
    if queue is None:
        queue = request.app.state.kiro_session_queue = KiroSessionQueue()

    async def import_completed_turn() -> dict[str, Any]:
        return await import_kiro_session(body, request)

    try:
        return queue.submit(body.session_id, body.through_turn_id, import_completed_turn)
    except RuntimeError as error:
        raise HTTPException(503, "Kiro Session synchronization queue is full") from error


@router.post("/recordings/kiro-session/queue/status")
async def queued_kiro_session_status(body: QueueKiroSession, request: Request) -> dict[str, Any]:
    _gate(request)
    queue = getattr(request.app.state, "kiro_session_queue", None)
    status = queue.status(body.session_id, body.through_turn_id) if queue is not None else None
    if status is None:
        raise HTTPException(404, "No queued synchronization for this Kiro turn")
    return status
