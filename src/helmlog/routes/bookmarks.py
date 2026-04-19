"""Route handlers for bookmarks (#477, Moments slice 1).

Bookmarks are timestamp-anchored moments on a session's timeline. Author or
admin can rename / delete; any authed user can list / create. All anchors go
through `helmlog.anchors.validate_anchor` for structural checks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from helmlog.anchors import Anchor, AnchorError, validate_anchor
from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


def _serialize(bm: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": bm["id"],
        "session_id": bm["session_id"],
        "created_by": bm["created_by"],
        "name": bm["name"],
        "note": bm["note"],
        "t_start": bm["anchor_t_start"],
        "created_at": bm["created_at"],
        "updated_at": bm["updated_at"],
    }


@router.post("/api/sessions/{session_id}/bookmarks", status_code=201)
async def api_create_bookmark(
    request: Request,
    session_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)

    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.json()
    name = (body.get("name") or "").strip()
    note_raw = body.get("note")
    note = note_raw.strip() if isinstance(note_raw, str) and note_raw.strip() else None
    t_start = body.get("t_start")

    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not isinstance(t_start, str) or not t_start:
        raise HTTPException(status_code=422, detail="t_start is required")

    try:
        validate_anchor(Anchor(kind="timestamp", t_start=t_start))
    except AnchorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    bm_id = await storage.create_bookmark(
        session_id=session_id,
        user_id=user.get("id"),
        name=name,
        note=note,
        t_start=t_start,
    )
    await audit(
        request,
        "bookmark.create",
        detail=f"bookmark={bm_id} session={session_id}",
        user=user,
    )
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    return JSONResponse(_serialize(bm), status_code=201)


@router.get("/api/sessions/{session_id}/bookmarks")
async def api_list_bookmarks(
    request: Request,
    session_id: int,
    tags: str | None = None,
    tag_mode: str = "and",
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List bookmarks on a session.

    Response: `{bookmarks: [...], available_tags: [...]}`. Each bookmark
    carries a `tags` array; `available_tags` is computed across the
    pre-tag-filter set so the chip row stays populated when a tag is
    active. `?tags=1,2&tag_mode=and|or` narrows the list.
    """
    storage = get_storage(request)

    session = await storage.get_race(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    rows = await storage.list_bookmarks_for_session(session_id)
    bm_ids = [r["id"] for r in rows]
    tag_map = await storage.list_tags_for_entities("bookmark", bm_ids)
    for r in rows:
        r["tags"] = tag_map.get(r["id"], [])

    # available_tags computed from the pre-tag-filter set so chip row
    # offers every tag a user could add to narrow further.
    available_counts: dict[int, dict[str, Any]] = {}
    for r in rows:
        for t in r.get("tags") or []:
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
                    await storage.list_entities_with_tags("bookmark", tag_ids, mode=tag_mode)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            rows = [r for r in rows if r["id"] in allowed]

    serialized = [{**_serialize(r), "tags": r.get("tags") or []} for r in rows]
    return JSONResponse({"bookmarks": serialized, "available_tags": available_tags})


def _may_modify(user: dict[str, Any], bm: dict[str, Any]) -> bool:
    if user.get("role") == "admin":
        return True
    return bm["created_by"] is not None and bm["created_by"] == user.get("id")


@router.patch("/api/bookmarks/{bookmark_id}")
async def api_update_bookmark(
    request: Request,
    bookmark_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    bm = await storage.get_bookmark(bookmark_id)
    if bm is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if not _may_modify(user, bm):
        raise HTTPException(status_code=403, detail="Only the author or admin can edit")

    body = await request.json()
    name_in = body.get("name")
    note_in = body.get("note", ...)  # sentinel: absent vs. explicit None

    name = name_in.strip() if isinstance(name_in, str) else None
    if name is not None and not name:
        raise HTTPException(status_code=422, detail="name must not be empty")

    clear_note = False
    note: str | None = None
    if note_in is not ...:
        if note_in is None:
            clear_note = True
        elif isinstance(note_in, str):
            stripped = note_in.strip()
            if not stripped:
                clear_note = True
            else:
                note = stripped
        else:
            raise HTTPException(status_code=422, detail="note must be a string or null")

    await storage.update_bookmark(bookmark_id, name=name, note=note, clear_note=clear_note)
    await audit(
        request,
        "bookmark.update",
        detail=f"bookmark={bookmark_id}",
        user=user,
    )
    updated = await storage.get_bookmark(bookmark_id)
    assert updated is not None
    return JSONResponse(_serialize(updated))


@router.delete("/api/bookmarks/{bookmark_id}", status_code=204)
async def api_delete_bookmark(
    request: Request,
    bookmark_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    bm = await storage.get_bookmark(bookmark_id)
    if bm is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if not _may_modify(user, bm):
        raise HTTPException(status_code=403, detail="Only the author or admin can delete")

    await storage.delete_bookmark(bookmark_id)
    await audit(
        request,
        "bookmark.delete",
        detail=f"bookmark={bookmark_id} session={bm['session_id']}",
        user=user,
    )
    return Response(status_code=204)
