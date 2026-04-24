"""Route handlers for push-mode ESP32-CAM devices (#660).

Battery-powered cameras can't keep a WiFi association for pull-mode capture,
so they wake on a timer, check if a race is running, optionally upload a
photo, and return to deep sleep. Two endpoints support that cycle:

- ``GET /api/device-cameras/status`` — cheap "should I bother capturing" probe
- ``POST /api/device-cameras/{role}/photo`` — multipart JPEG ingest

The URL prefix is ``/api/device-cameras/`` rather than ``/api/cameras/`` (as the
issue initially drafted) to avoid collision with the existing Insta360 control
routes in ``routes/cameras.py``.

Both are auth'd by device bearer token (``require_auth("crew")``). Photos
are stored alongside regular photo notes under ``NOTES_DIR`` so they appear
in the session's notes list and are served by the existing ``/notes/{path}``
route.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/device-cameras/status")
async def camera_status(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Return whether a race is currently active, so sleepy cameras can skip upload."""
    storage = get_storage(request)
    current = await storage.get_current_race()
    if current is None:
        return JSONResponse({"active": False, "session_id": None})
    return JSONResponse({"active": True, "session_id": current.id})


@router.post("/api/device-cameras/{role}/photo")
async def camera_photo(
    request: Request,
    role: str,
    file: UploadFile,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    """Ingest a JPEG from an ESP32-CAM, attach it to the current race as a photo note.

    The ``{role}`` path param must match the authenticated device's ``name`` — a
    camera can only post as itself. With no active race, returns 204 and does
    not write the file (firmware treats 204 as "ignore, back to sleep").
    """
    if _user.get("name") != role:
        raise HTTPException(status_code=403, detail="Role does not match authenticated device")

    storage = get_storage(request)
    current = await storage.get_current_race()
    if current is None:
        return Response(status_code=204)

    race_id = current.id
    notes_dir = os.environ.get("NOTES_DIR", "data/notes")
    session_dir = Path(notes_dir) / str(race_id)
    await asyncio.to_thread(session_dir.mkdir, parents=True, exist_ok=True)

    ts = datetime.now(UTC).isoformat()
    safe_ts = ts.replace(":", "-").replace("+", "")[:19]
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    filename = f"{role}_{safe_ts}_{uuid.uuid4().hex[:8]}{ext}"
    dest = session_dir / filename

    data = await file.read()
    await asyncio.to_thread(dest.write_bytes, data)

    photo_path = f"{race_id}/{filename}"
    note_id = await storage.create_note(
        ts,
        None,
        race_id=race_id,
        audio_session_id=None,
        note_type="photo",
        photo_path=photo_path,
        user_id=_user.get("id"),
    )
    from helmlog import influx

    await asyncio.to_thread(
        influx.write_note,
        ts_iso=ts,
        note_type="photo",
        body=f"/notes/{photo_path}",
        race_id=race_id,
        note_id=note_id,
    )
    await audit(request, "camera.photo", detail=photo_path, user=_user)
    return JSONResponse(
        {"id": note_id, "ts": ts, "photo_path": photo_path},
        status_code=201,
    )
