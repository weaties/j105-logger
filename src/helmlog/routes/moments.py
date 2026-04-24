"""Route handlers for moments (#662 moments unification).

Moments replace the prior bookmarks / comment-threads / session-notes
primitives with a single anchored annotation that can carry subject,
counterparty, tags, comments, and attachments. See `helmlog.storage`
for the storage API and the v80 migration for the data model.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from helmlog.routes._helpers import audit, get_storage
from helmlog.storage import AnchorScopeError

router = APIRouter()

_VALID_ANCHOR_KINDS = {"session", "timestamp", "maneuver", "transcript_segment"}
_ATTACHMENT_DIR_ENV = "ATTACHMENTS_DIR"
_LEGACY_NOTES_DIR_ENV = "NOTES_DIR"


def _attachments_dir() -> Path:
    """Resolve the attachments directory, honoring the old NOTES_DIR env
    var as a fallback so existing Pi deployments keep serving their
    migrated photo files without reconfiguration."""
    raw = os.environ.get(_ATTACHMENT_DIR_ENV) or os.environ.get(_LEGACY_NOTES_DIR_ENV, "data/notes")
    return Path(raw)


def _moment_is_author(user: dict[str, Any], moment: dict[str, Any]) -> bool:
    if user.get("role") == "admin":
        return True
    return moment["created_by"] is not None and moment["created_by"] == user.get("id")


# ---------------------------------------------------------------------------
# Create / list
# ---------------------------------------------------------------------------


@router.post("/api/sessions/{session_id}/moments", status_code=201)
async def api_create_moment(
    request: Request,
    session_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Create a moment. Body:
    ``{anchor_kind, anchor_entity_id?, t_start?, t_end?, subject?,
       counterparty?, first_comment?, tag_ids?}``. ``source`` is forced
    to ``manual`` — auto-tagger paths go through internal APIs."""
    storage = get_storage(request)
    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.json()
    anchor_kind = body.get("anchor_kind")
    if anchor_kind not in _VALID_ANCHOR_KINDS:
        raise HTTPException(status_code=422, detail="invalid anchor_kind")

    anchor_entity_id = body.get("anchor_entity_id")
    t_start = body.get("t_start")
    t_end = body.get("t_end")
    subject_in = body.get("subject")
    subject = subject_in.strip() if isinstance(subject_in, str) and subject_in.strip() else None
    cp_in = body.get("counterparty")
    counterparty = cp_in.strip() if isinstance(cp_in, str) and cp_in.strip() else None
    first_comment = body.get("first_comment")
    tag_ids = body.get("tag_ids") or []

    try:
        await storage._assert_moment_anchor_in_session(anchor_kind, anchor_entity_id, session_id)
    except AnchorScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        moment_id = await storage.create_moment(
            session_id=session_id,
            anchor_kind=anchor_kind,
            anchor_entity_id=anchor_entity_id,
            anchor_t_start=t_start,
            anchor_t_end=t_end,
            subject=subject,
            counterparty=counterparty,
            user_id=user.get("id"),
            source="manual",
        )
    except Exception as exc:  # AnchorError from storage
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if isinstance(first_comment, str) and first_comment.strip():
        await storage.create_comment(moment_id, user["id"], first_comment.strip())

    if isinstance(tag_ids, list):
        for tid in tag_ids:
            if isinstance(tid, int):
                with contextlib.suppress(Exception):
                    await storage.attach_tag("moment", moment_id, tid, user_id=user.get("id"))

    await audit(
        request,
        "moment.create",
        detail=f"moment={moment_id} session={session_id}",
        user=user,
    )

    from helmlog.notifications import notify_new_moment

    await notify_new_moment(storage, moment_id, session_id, user["id"])
    m = await storage.get_moment(moment_id)
    assert m is not None
    return JSONResponse(m, status_code=201)


@router.get("/api/sessions/{session_id}/moments")
async def api_list_moments(
    request: Request,
    session_id: int,
    tags: str | None = None,
    tag_mode: str = "and",
    include_unconfirmed: bool = False,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    moments = await storage.list_moments_for_session(
        session_id, include_unconfirmed=include_unconfirmed
    )
    moment_ids = [m["id"] for m in moments]
    tag_map = await storage.list_tags_for_entities("moment", moment_ids)
    for m in moments:
        m["tags"] = tag_map.get(m["id"], [])

    available_counts: dict[int, dict[str, Any]] = {}
    for m in moments:
        for t in m.get("tags") or []:
            entry = available_counts.setdefault(
                t["id"],
                {"id": t["id"], "name": t["name"], "color": t["color"], "count": 0},
            )
            entry["count"] += 1
    available_tags = sorted(available_counts.values(), key=lambda t: t["name"])

    if tags:
        try:
            tag_ids = [int(s) for s in tags.split(",") if s.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="tags must be comma-separated ints"
            ) from exc
        if tag_ids:
            try:
                allowed = set(
                    await storage.list_entities_with_tags("moment", tag_ids, mode=tag_mode)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            moments = [m for m in moments if m["id"] in allowed]

    return JSONResponse({"moments": moments, "available_tags": available_tags})


@router.get("/api/sessions/{session_id}/anchors")
async def api_list_session_anchors(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    anchors = await storage.list_session_anchors(session_id)
    return JSONResponse(anchors)


# ---------------------------------------------------------------------------
# Typeahead / aggregation endpoints — declared BEFORE the /{moment_id} route
# so FastAPI matches the literal path first.
# ---------------------------------------------------------------------------


@router.get("/api/moments/counterparties")
async def api_moment_counterparties(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    return JSONResponse({"counterparties": await storage.list_moment_counterparties()})


# ---------------------------------------------------------------------------
# Moment detail / update / delete
# ---------------------------------------------------------------------------


@router.get("/api/moments/{moment_id}")
async def api_get_moment(
    request: Request,
    moment_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    return JSONResponse(m)


@router.patch("/api/moments/{moment_id}")
async def api_update_moment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    if not _moment_is_author(user, m):
        raise HTTPException(status_code=403, detail="Only the author or admin can edit")

    body = await request.json()
    subject_in = body.get("subject", ...)
    cp_in = body.get("counterparty", ...)

    clear_subject = False
    subject: str | None = None
    if subject_in is not ...:
        if subject_in is None or (isinstance(subject_in, str) and not subject_in.strip()):
            clear_subject = True
        elif isinstance(subject_in, str):
            subject = subject_in.strip()
        else:
            raise HTTPException(status_code=422, detail="subject must be string or null")

    clear_counterparty = False
    counterparty: str | None = None
    if cp_in is not ...:
        if cp_in is None or (isinstance(cp_in, str) and not cp_in.strip()):
            clear_counterparty = True
        elif isinstance(cp_in, str):
            counterparty = cp_in.strip()
        else:
            raise HTTPException(status_code=422, detail="counterparty must be string or null")

    await storage.update_moment(
        moment_id,
        subject=subject,
        counterparty=counterparty,
        clear_subject=clear_subject,
        clear_counterparty=clear_counterparty,
    )
    await audit(request, "moment.update", detail=f"moment={moment_id}", user=user)
    updated = await storage.get_moment(moment_id)
    return JSONResponse(updated)


@router.delete("/api/moments/{moment_id}", status_code=204)
async def api_delete_moment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    if not _moment_is_author(user, m):
        raise HTTPException(status_code=403, detail="Only the author or admin can delete")

    attachment_paths = [a.get("path") for a in m.get("attachments", []) if a.get("path")]
    await storage.delete_moment(moment_id)
    await audit(
        request,
        "moment.delete",
        detail=f"moment={moment_id} session={m['session_id']}",
        user=user,
    )
    # Tidy up disk files for any attachments.
    for rel in attachment_paths:
        full = _attachments_dir() / rel
        if full.exists():
            with contextlib.suppress(OSError):
                await asyncio.to_thread(full.unlink)
    return Response(status_code=204)


@router.post("/api/moments/{moment_id}/resolve")
async def api_resolve_moment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    if not _moment_is_author(user, m):
        raise HTTPException(status_code=403, detail="Only the moment creator or admin can resolve")
    body = await request.json()
    summary: str | None = body.get("resolution_summary")
    await storage.resolve_moment(moment_id, user["id"], summary)
    await audit(request, "moment.resolve", detail=f"moment={moment_id}", user=user)
    from helmlog.notifications import notify_resolved

    await notify_resolved(storage, moment_id, m["session_id"], user["id"])
    return JSONResponse({"ok": True})


@router.post("/api/moments/{moment_id}/unresolve")
async def api_unresolve_moment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    if not _moment_is_author(user, m):
        raise HTTPException(
            status_code=403, detail="Only the moment creator or admin can unresolve"
        )
    await storage.unresolve_moment(moment_id)
    await audit(request, "moment.unresolve", detail=f"moment={moment_id}", user=user)
    return JSONResponse({"ok": True})


@router.post("/api/moments/{moment_id}/read")
async def api_mark_moment_read(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    await storage.mark_moment_read(moment_id, user["id"])
    return JSONResponse({"ok": True})


@router.post("/api/moments/{moment_id}/confirm")
async def api_confirm_moment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    await storage.confirm_moment(moment_id, user["id"])
    await audit(request, "moment.confirm", detail=f"moment={moment_id}", user=user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Comments on a moment
# ---------------------------------------------------------------------------


@router.post("/api/moments/{moment_id}/comments", status_code=201)
async def api_create_comment(
    request: Request,
    moment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    body = await request.json()
    text: str = body.get("body", "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="body is required")
    comment_id = await storage.create_comment(moment_id, user["id"], text)
    await audit(
        request, "comment.create", detail=f"comment={comment_id} moment={moment_id}", user=user
    )
    from helmlog.notifications import notify_mention, notify_reply, parse_mentions

    session_id = m["session_id"]
    all_users = await storage.list_users()
    known_names = [u["name"] for u in all_users if u.get("name")]
    mentioned_names = parse_mentions(text, known_names=known_names)
    if mentioned_names:
        name_map = await storage.resolve_user_names(mentioned_names)
        if name_map:
            await notify_mention(
                storage, comment_id, moment_id, session_id, user["id"], list(name_map.values())
            )
    await notify_reply(storage, comment_id, moment_id, session_id, user["id"])
    return JSONResponse({"id": comment_id}, status_code=201)


# ---------------------------------------------------------------------------
# Attachments (photos etc.)
# ---------------------------------------------------------------------------


@router.post("/api/moments/{moment_id}/attachments", status_code=201)
async def api_create_photo_attachment(
    request: Request,
    moment_id: int,
    file: UploadFile,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    m = await storage.get_moment(moment_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Moment not found")

    session_id = m["session_id"]
    base = _attachments_dir() / str(session_id)
    await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)

    now_iso = datetime.now(UTC).isoformat()
    safe = now_iso.replace(":", "-").replace("+", "")[:19]
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    filename = f"{safe}_{uuid.uuid4().hex[:8]}{ext}"
    dest = base / filename
    data = await file.read()
    await asyncio.to_thread(dest.write_bytes, data)

    rel = f"{session_id}/{filename}"
    attachment_id = await storage.create_attachment(
        moment_id=moment_id,
        kind="photo",
        path=rel,
        user_id=_user.get("id"),
    )
    await audit(request, "moment.attach", detail=rel, user=_user)
    return JSONResponse(
        {"id": attachment_id, "moment_id": moment_id, "kind": "photo", "path": rel},
        status_code=201,
    )


@router.delete("/api/attachments/{attachment_id}", status_code=204)
async def api_delete_attachment(
    request: Request,
    attachment_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    found, path = await storage.delete_attachment_with_file(attachment_id)
    if not found:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if path:
        full = _attachments_dir() / path
        if full.exists():
            try:
                await asyncio.to_thread(full.unlink)
                logger.info("Deleted attachment file: {}", full)
            except OSError:
                logger.warning("Could not delete attachment file: {}", full)
    await audit(request, "attachment.delete", detail=str(attachment_id), user=_user)


@router.get("/attachments/{path:path}")
async def serve_attachment(
    path: str,
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    get_storage(request)
    base = _attachments_dir().resolve()
    full = (base / path).resolve()
    if not str(full).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not full.exists():
        raise HTTPException(status_code=404, detail="Not found")
    st = full.stat()
    etag = f'"{st.st_mtime_ns}-{st.st_size}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)
    return FileResponse(
        full,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


# ---------------------------------------------------------------------------
# Session settings (was note_type='settings')
# ---------------------------------------------------------------------------


@router.post("/api/sessions/{session_id}/settings", status_code=201)
async def api_create_session_setting(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    body = await request.json()
    raw_body = body.get("body")
    ts = body.get("ts")
    if not isinstance(raw_body, str) or not raw_body.strip():
        raise HTTPException(status_code=422, detail="body must be a non-empty JSON string")
    try:
        parsed = json.loads(raw_body)
        if not isinstance(parsed, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(  # noqa: B904
            status_code=422, detail="body must be a JSON object"
        )
    setting_id = await storage.create_session_setting(
        session_id=session_id, body=raw_body, user_id=_user.get("id"), ts=ts
    )
    await audit(request, "session_setting.create", detail=str(setting_id), user=_user)
    return JSONResponse({"id": setting_id}, status_code=201)


@router.get("/api/session-settings/keys")
async def api_session_settings_keys(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    return JSONResponse({"keys": await storage.list_settings_keys()})


# ---------------------------------------------------------------------------
# Photo backward-compat alias: /api/sessions/{id}/notes/photo continues to
# work so existing session.js photo upload keeps functioning until the UI
# rework lands. Internally creates a timestamp-anchored moment + photo
# attachment.
# ---------------------------------------------------------------------------


@router.post("/api/sessions/{session_id}/notes/photo", status_code=201)
async def api_legacy_photo_upload(
    request: Request,
    session_id: int,
    file: UploadFile,
    ts: str = Form(default=""),
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Legacy photo-upload shim (#662). Creates a timestamp-anchored moment
    and attaches the uploaded photo. Returns a payload compatible with the
    previous `notes/photo` contract so the old UI keeps working."""
    storage = get_storage(request)
    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    now_iso = datetime.now(UTC).isoformat()
    actual_ts = ts.strip() if ts.strip() else now_iso
    base = _attachments_dir() / str(session_id)
    await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
    safe = actual_ts.replace(":", "-").replace("+", "")[:19]
    ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
    filename = f"{safe}_{uuid.uuid4().hex[:8]}{ext}"
    dest = base / filename
    data = await file.read()
    await asyncio.to_thread(dest.write_bytes, data)

    moment_id = await storage.create_moment(
        session_id=session_id,
        anchor_kind="timestamp",
        anchor_t_start=actual_ts,
        user_id=_user.get("id"),
    )
    rel = f"{session_id}/{filename}"
    await storage.create_attachment(
        moment_id=moment_id, kind="photo", path=rel, user_id=_user.get("id")
    )
    await audit(request, "moment.photo", detail=rel, user=_user)
    return JSONResponse(
        {"id": moment_id, "ts": actual_ts, "photo_path": rel},
        status_code=201,
    )
