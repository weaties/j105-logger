"""Route handlers for audio."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

# Expose asyncio on the helmlog.web module so tests can patch
# helmlog.web.asyncio.create_task (the original location before the router split).
import helmlog.web as _web_mod
from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, limiter

if TYPE_CHECKING:
    from helmlog.storage import Storage

_web_mod.asyncio = asyncio  # type: ignore[attr-defined]

router = APIRouter()


async def _resolve_transcription_targets(storage: Storage, row: dict[str, Any]) -> list[int]:
    """Return every audio_session_id that should be transcribed for this request.

    Single-device sessions return ``[row["id"]]``. Sibling-card captures
    (#509) return every member of the ``capture_group_id`` in ordinal
    order so fan-out transcribes the whole group in one click.
    """
    group_id = row.get("capture_group_id")
    if not group_id:
        return [int(row["id"])]
    siblings = await storage.list_capture_group_siblings(str(group_id))
    if not siblings:
        return [int(row["id"])]
    return [int(s["id"]) for s in siblings]


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

    from helmlog.storage import get_effective_setting
    from helmlog.transcribe import transcribe_session

    t_url = await get_effective_setting(storage, "TRANSCRIBE_URL")
    # When offloading to a remote worker, always request diarization —
    # the worker has its own HF_TOKEN and decides locally.  Only gate on
    # the Pi's HF_TOKEN when running the local fallback path.
    diarize = True if t_url else bool(os.environ.get("HF_TOKEN"))

    # Sibling-card capture (#509): fan out to every sibling in the group so
    # the merged transcript covers every receiver, not just the one the UI
    # happened to click on. Non-sibling sessions take the single-row path.
    targets = await _resolve_transcription_targets(storage, row)
    jobs: list[int] = []
    try:
        for tgt in targets:
            jobs.append(await storage.create_transcript_job(tgt, model))
    except ValueError:
        # Roll back any sibling jobs we already created so the group state is
        # consistent (all pending or none pending).
        for _created, t in zip(jobs, targets, strict=False):
            await storage.delete_transcript(t)
        raise HTTPException(  # noqa: B904
            status_code=409, detail="Transcript job already exists for this session"
        )

    for tgt, tid in zip(targets, jobs, strict=True):
        asyncio.create_task(
            transcribe_session(
                storage,
                tgt,
                tid,
                model_size=model,
                diarize=diarize,
                transcribe_url=t_url,
            )
        )
    await audit(request, "transcribe.start", detail=str(session_id), user=_user)
    primary_transcript_id = jobs[0] if jobs else None
    return JSONResponse(
        {
            "status": "accepted",
            "transcript_id": primary_transcript_id,
            "sibling_count": len(jobs),
        },
        status_code=202,
    )


@router.post("/api/audio/{session_id}/retranscribe", status_code=202)
@limiter.limit("5/minute")
async def api_retranscribe(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Delete existing transcript and retranscribe with current settings.

    Useful when a transcript was created without diarization and needs to be
    redone with speaker identification enabled.
    """
    storage = get_storage(request)
    row = await storage.get_audio_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Audio session not found")

    # Fan out to siblings (#509): delete + recreate jobs for every member.
    targets = await _resolve_transcription_targets(storage, row)
    for tgt in targets:
        await storage.delete_transcript(tgt)

    model = os.environ.get("WHISPER_MODEL", "base")
    jobs = [await storage.create_transcript_job(tgt, model) for tgt in targets]

    from helmlog.storage import get_effective_setting
    from helmlog.transcribe import transcribe_session

    t_url = await get_effective_setting(storage, "TRANSCRIBE_URL")
    # When offloading to a remote worker, always request diarization —
    # the worker has its own HF_TOKEN and decides locally.  Only gate on
    # the Pi's HF_TOKEN when running the local fallback path.
    diarize = True if t_url else bool(os.environ.get("HF_TOKEN"))
    for tgt, tid in zip(targets, jobs, strict=True):
        asyncio.create_task(
            transcribe_session(
                storage,
                tgt,
                tid,
                model_size=model,
                diarize=diarize,
                transcribe_url=t_url,
            )
        )
    await audit(request, "transcribe.retranscribe", detail=str(session_id), user=_user)
    return JSONResponse(
        {"status": "accepted", "transcript_id": jobs[0], "sibling_count": len(jobs)},
        status_code=202,
    )


@router.get("/api/audio/{session_id}/transcript")
async def api_get_transcript(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Poll transcription status and retrieve the transcript text when done.

    Applies speaker anonymization map if present (#197). When the target
    session is part of a sibling capture group (#509), the segments array
    is a merged, time-sorted union of every sibling's transcript so the
    UI sees one continuous timeline across all receivers.
    """
    storage = get_storage(request)
    import json as _json

    t = await storage.get_transcript_with_anon(session_id)
    if t is None:
        raise HTTPException(status_code=404, detail="No transcript job found for this session")

    # Sibling merge: if this session has a capture_group_id, union the
    # segments from every sibling's transcript into a single sorted array,
    # AND union their speaker_maps (#648) so assignments on any sibling are
    # visible in the merged view. Labels are globally unique because we
    # prefix with position_name in _persist_sibling_segments.
    row = await storage.get_audio_session_row(session_id)
    group_id = row.get("capture_group_id") if row else None
    merged_speaker_map: dict[str, Any] = {}
    if group_id:
        merged: list[dict[str, Any]] = []
        siblings = await storage.list_capture_group_siblings(str(group_id))
        for sr in siblings:
            st = await storage.get_transcript_with_anon(int(sr["id"]))
            if st is None:
                continue
            sj = st.get("segments_json")
            if sj:
                try:
                    segs = _json.loads(sj)
                    merged.extend(segs)
                except (_json.JSONDecodeError, TypeError):
                    pass
            sm = st.get("speaker_map")
            if sm:
                import contextlib

                with contextlib.suppress(_json.JSONDecodeError, TypeError):
                    merged_speaker_map.update(_json.loads(sm))
        merged.sort(key=lambda s: float(s.get("start", 0.0)))
        t["segments"] = merged
    elif t.get("segments_json"):
        t["segments"] = _json.loads(t["segments_json"])
    t.pop("segments_json", None)
    # Remove internal anon map from response
    t.pop("speaker_anon_map", None)
    # Expose speaker_map for UI (crew labels, auto-match info)
    raw_map = t.pop("speaker_map", None)
    if group_id:
        t["speaker_map"] = merged_speaker_map
    else:
        t["speaker_map"] = _json.loads(raw_map) if raw_map else {}
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


@router.post("/api/audio/{session_id}/transcript/assign-speaker", status_code=200)
@limiter.limit("30/minute")
async def api_assign_speaker(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Assign a speaker label to a crew member in a diarized transcript."""
    storage = get_storage(request)
    body = await request.json()
    speaker_label = (body.get("speaker_label") or "").strip()
    user_id = body.get("user_id")
    if not speaker_label:
        raise HTTPException(status_code=422, detail="speaker_label is required")
    if user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    t = await storage.get_transcript(session_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    # Look up the user name
    db = storage._conn()
    cur = await db.execute("SELECT name FROM users WHERE id = ?", (user_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    name = row["name"] or f"User {user_id}"
    # #648: fan out across siblings so the merged transcript's speaker_map
    # gets updated regardless of which sibling's primary we POSTed against.
    # Speaker labels are globally unique (prefixed with position_name) so
    # writing the same label across every sibling's speaker_map is safe.
    asession = await storage.get_audio_session_row(session_id)
    group_id = asession.get("capture_group_id") if asession else None
    targets: list[int] = []
    if group_id:
        siblings = await storage.list_capture_group_siblings(str(group_id))
        for sr in siblings:
            st = await storage.get_transcript(int(sr["id"]))
            if st is not None:
                targets.append(int(st["id"]))
    if not targets:
        targets = [int(t["id"])]
    any_found = False
    for tid in targets:
        if await storage.assign_speaker_crew(tid, speaker_label, user_id, name):
            any_found = True
    if not any_found:
        raise HTTPException(status_code=404, detail="Transcript not found")
    await audit(
        request,
        "transcript.assign_speaker",
        detail=f"session={session_id} speaker={speaker_label} user={user_id}",
        user=_user,
    )
    # Trigger voice profile build check in background (non-blocking)
    from helmlog.transcribe import maybe_build_voice_profile

    asyncio.create_task(maybe_build_voice_profile(storage, user_id))
    return JSONResponse({"speaker_label": speaker_label, "user_id": user_id, "name": name})


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
