"""Route handlers for notes."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from helmlog.auth import require_auth
from helmlog.routes._helpers import NoteCreate, audit, get_storage

router = APIRouter()


async def _resolve_session(request: Request, session_id: int) -> tuple[int | None, int | None]:
    """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is not None:
        return session_id, None
    cur2 = await storage._conn().execute(
        "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
    )
    if await cur2.fetchone() is not None:
        return None, session_id
    raise HTTPException(status_code=404, detail="Session not found")


@router.post("/api/sessions/{session_id}/notes", status_code=201)
async def api_create_note(
    request: Request,
    session_id: int,
    body: NoteCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    if body.note_type not in ("text", "settings"):
        raise HTTPException(status_code=422, detail="note_type must be 'text' or 'settings'")
    if body.note_type == "text" and (not body.body or not body.body.strip()):
        raise HTTPException(status_code=422, detail="body must not be blank for text notes")
    if body.note_type == "settings":
        if not body.body:
            raise HTTPException(status_code=422, detail="body must not be blank for settings notes")
        try:
            parsed = json.loads(body.body)
            if not isinstance(parsed, dict):
                raise ValueError  # noqa: TRY301
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(  # noqa: B904
                status_code=422,
                detail="body must be a JSON object for settings notes",
            )
    race_id, audio_session_id = await _resolve_session(request, session_id)
    ts = body.ts if body.ts else datetime.now(UTC).isoformat()
    note_id = await storage.create_note(
        ts,
        body.body,
        race_id=race_id,
        audio_session_id=audio_session_id,
        note_type=body.note_type,
        user_id=_user.get("id"),
    )
    from helmlog import influx

    await asyncio.to_thread(
        influx.write_note,
        ts_iso=ts,
        note_type=body.note_type,
        body=body.body,
        race_id=race_id,
        note_id=note_id,
    )
    await audit(request, "note.add", detail=body.note_type, user=_user)
    return JSONResponse({"id": note_id, "ts": ts}, status_code=201)


@router.post("/api/sessions/{session_id}/notes/photo", status_code=201)
async def api_create_photo_note(
    request: Request,
    session_id: int,
    file: UploadFile,
    ts: str = Form(default=""),
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    race_id, audio_session_id = await _resolve_session(request, session_id)

    notes_dir = os.environ.get("NOTES_DIR", "data/notes")
    session_dir = Path(notes_dir) / str(session_id)
    await asyncio.to_thread(session_dir.mkdir, parents=True, exist_ok=True)

    now_str = datetime.now(UTC).isoformat()
    actual_ts = ts.strip() if ts.strip() else now_str
    safe_ts = actual_ts.replace(":", "-").replace("+", "")[:19]
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    filename = f"{safe_ts}_{uuid.uuid4().hex[:8]}{ext}"
    dest = session_dir / filename

    data = await file.read()
    await asyncio.to_thread(dest.write_bytes, data)

    photo_path = f"{session_id}/{filename}"
    note_id = await storage.create_note(
        actual_ts,
        None,
        race_id=race_id,
        audio_session_id=audio_session_id,
        note_type="photo",
        photo_path=photo_path,
        user_id=_user.get("id"),
    )
    from helmlog import influx

    await asyncio.to_thread(
        influx.write_note,
        ts_iso=actual_ts,
        note_type="photo",
        body=f"/notes/{photo_path}",
        race_id=race_id,
        note_id=note_id,
    )
    await audit(request, "note.photo", detail=photo_path, user=_user)
    return JSONResponse({"id": note_id, "ts": actual_ts, "photo_path": photo_path}, status_code=201)


@router.get("/notes/{path:path}")
async def serve_note_photo(
    path: str,
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    get_storage(request)
    notes_dir = Path(os.environ.get("NOTES_DIR", "data/notes")).resolve()
    full_path = (notes_dir / path).resolve()
    if not str(full_path).startswith(str(notes_dir)):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    st = full_path.stat()
    etag = f'"{st.st_mtime_ns}-{st.st_size}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)
    return FileResponse(
        full_path,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


@router.get("/api/sessions/{session_id}/notes")
async def api_list_notes(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    race_id, audio_session_id = await _resolve_session(request, session_id)
    notes = await storage.list_notes(race_id=race_id, audio_session_id=audio_session_id)
    return JSONResponse(notes)


@router.delete("/api/notes/{note_id}", status_code=204)
async def api_delete_note(
    request: Request,
    note_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    found, photo_path = await storage.delete_note_with_file(note_id)
    if not found:
        raise HTTPException(status_code=404, detail="Note not found")
    if photo_path:
        # Clean up the physical photo file (#205)
        notes_dir = Path(os.environ.get("NOTES_DIR", "data/notes"))
        full_path = notes_dir / photo_path
        if full_path.exists():
            await asyncio.to_thread(full_path.unlink)
            logger.info("Deleted photo file: {}", full_path)
    await audit(request, "note.delete", detail=str(note_id), user=_user)


@router.get("/api/notes/settings-keys")
async def api_settings_keys(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return all distinct keys used in settings notes, sorted alphabetically.

    Used to populate the typeahead datalist on the settings note entry form.
    Returns: {"keys": ["backstay", "cunningham", ...]}
    """
    storage = get_storage(request)
    keys = await storage.list_settings_keys()
    return JSONResponse({"keys": keys})


# ------------------------------------------------------------------
# /api/sessions/{session_id}/videos  &  /api/videos/{video_id}
# ------------------------------------------------------------------


def _video_deep_link(row: dict[str, Any], at_utc: datetime | None = None) -> dict[str, Any]:
    """Augment a race_videos row with a computed YouTube deep-link.

    If *at_utc* is supplied the link jumps to that moment in the video.
    Otherwise the link just opens the video from the beginning.
    """
    from helmlog.video import VideoSession  # local import to avoid circular deps

    sync_utc = datetime.fromisoformat(row["sync_utc"])
    duration_s = row["duration_s"]

    out = dict(row)
    if at_utc is not None and duration_s is not None:
        vs = VideoSession(
            url=row["youtube_url"],
            video_id=row["video_id"],
            title=row["title"],
            duration_s=duration_s,
            sync_utc=sync_utc,
            sync_offset_s=row["sync_offset_s"],
        )
        out["deep_link"] = vs.url_at(at_utc)
    else:
        out["deep_link"] = None
    return out
