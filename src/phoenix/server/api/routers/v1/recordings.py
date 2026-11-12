from dataclasses import asdict
from ipaddress import ip_address
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

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
