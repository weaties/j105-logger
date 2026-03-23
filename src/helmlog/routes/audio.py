"""Route handlers for audio."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

# Expose asyncio on the helmlog.web module so tests can patch
# helmlog.web.asyncio.create_task (the original location before the router split).
import helmlog.web as _web_mod
from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, limiter

_web_mod.asyncio = asyncio  # type: ignore[attr-defined]

router = APIRouter()


@router.get("/api/audio/{session_id}/download")
@limiter.limit("10/minute")
async def download_audio(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> FileResponse:
    """Download a WAV file as an attachment."""
    storage = get_storage(request)
    row = await storage.get_audio_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audio session not found")
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")
    await audit(request, "audio.download", detail=str(session_id), user=_user)
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@router.get("/api/audio/{session_id}/stream")
@limiter.limit("30/minute")
async def stream_audio(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> FileResponse:
    """Stream a WAV file; Starlette handles Range headers for seekable playback."""
    storage = get_storage(request)
    row = await storage.get_audio_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audio session not found")
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")
    await audit(request, "audio.stream", detail=str(session_id), user=_user)
    return FileResponse(path, media_type="audio/wav")


@router.post("/api/audio/{session_id}/transcribe", status_code=202)
async def api_transcribe(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Trigger a transcription job for an audio session (202 Accepted).

    If a job already exists, returns 409 Conflict.
    """
    storage = get_storage(request)
    row = await storage.get_audio_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audio session not found")
    model = os.environ.get("WHISPER_MODEL", "base")
    try:
        transcript_id = await storage.create_transcript_job(session_id, model)
    except ValueError:
        raise HTTPException(  # noqa: B904
            status_code=409, detail="Transcript job already exists for this session"
        )

    from helmlog.storage import get_effective_setting
    from helmlog.transcribe import transcribe_session

    t_url = await get_effective_setting(storage, "TRANSCRIBE_URL")
    diarize = bool(os.environ.get("HF_TOKEN"))
    asyncio.create_task(
        transcribe_session(
            storage,
            session_id,
            transcript_id,
            model_size=model,
            diarize=diarize,
            transcribe_url=t_url,
        )
    )
    await audit(request, "transcribe.start", detail=str(session_id), user=_user)
    return JSONResponse({"status": "accepted", "transcript_id": transcript_id}, status_code=202)


@router.get("/api/audio/{session_id}/transcript")
async def api_get_transcript(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Poll transcription status and retrieve the transcript text when done.

    Applies speaker anonymization map if present (#197).
    """
    storage = get_storage(request)
    import json as _json

    t = await storage.get_transcript_with_anon(session_id)
    if t is None:
        raise HTTPException(status_code=404, detail="No transcript job found for this session")
    if t.get("segments_json"):
        t["segments"] = _json.loads(t["segments_json"])
    del t["segments_json"]
    # Remove internal anon map from response
    t.pop("speaker_anon_map", None)
    return JSONResponse(t)


@router.delete("/api/audio/{session_id}", status_code=204)
async def api_delete_audio(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Delete an audio session and its WAV file."""
    storage = get_storage(request)
    file_path = await storage.delete_audio_session(session_id)
    if file_path is None:
        raise HTTPException(status_code=404, detail="Audio session not found")
    p = Path(file_path)
    if p.exists():
        await asyncio.to_thread(p.unlink)
        logger.info("Deleted audio file: {}", p)
    await audit(request, "audio.delete", detail=str(session_id), user=_user)


@router.post("/api/audio/{session_id}/transcript/anonymize-speaker", status_code=200)
async def api_anonymize_speaker(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Anonymize a speaker label in a diarized transcript."""
    storage = get_storage(request)
    body = await request.json()
    speaker_label = (body.get("speaker_label") or "").strip()
    if not speaker_label:
        raise HTTPException(status_code=422, detail="speaker_label is required")
    # Find the transcript for this audio session
    t = await storage.get_transcript(session_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    found = await storage.anonymize_speaker(t["id"], speaker_label)
    if not found:
        raise HTTPException(status_code=404, detail="Transcript not found")
    await audit(
        request,
        "transcript.anonymize_speaker",
        detail=f"session={session_id} speaker={speaker_label}",
        user=_user,
    )
    return JSONResponse({"anonymized": speaker_label})
