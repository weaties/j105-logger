"""Route handlers for tags (#587 / #588 slice 3).

Tag CRUD + merge live under /api/tags; attach/detach/list go through the
polymorphic /api/entities/{entity_type}/{entity_id}/tags path. The old
per-entity routes (/api/sessions/{id}/tags, /api/notes/{id}/tags) are
back-compat shims so existing UI keeps working through this PR — they
delegate to the generic attach/detach.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage
from helmlog.storage import ENTITY_TYPES

router = APIRouter()


# ---------------------------------------------------------------------------
# /api/tags — CRUD + merge
# ---------------------------------------------------------------------------


@router.get("/api/tags")
async def api_list_tags(
    request: Request,
    order_by: str = "name",
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    if order_by not in {"name", "usage"}:
        raise HTTPException(status_code=400, detail="order_by must be 'name' or 'usage'")
    storage = get_storage(request)
    tags = await storage.list_tags(order_by=order_by)
    return JSONResponse(tags)


@router.post("/api/tags", status_code=201)
async def api_create_tag(
    request: Request,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Any authed user can create tags (supports inline-create UX)."""
    storage = get_storage(request)
    name = (body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    color = body.get("color")
    tag = await storage.get_tag_by_name(name)
    if tag:
        raise HTTPException(status_code=409, detail="Tag already exists")
    tag_id = await storage.create_tag(name, color)
    await audit(request, "tag.create", detail=name, user=_user)
    return JSONResponse({"id": tag_id, "name": name, "color": color}, status_code=201)


@router.patch("/api/tags/{tag_id}", status_code=200)
async def api_update_tag(
    request: Request,
    tag_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Admin only — rename/recolor affects the shared tag namespace.

    ``{"color": null}`` in the body clears the color back to NULL; omitting
    the key leaves color unchanged.
    """
    storage = get_storage(request)
    # Sentinel to distinguish "color absent" from "color: null".
    _missing = object()
    color_in = body.get("color", _missing)
    clear_color = color_in is None and "color" in body
    color: str | None = color_in if isinstance(color_in, str) else None
    found = await storage.update_tag(
        tag_id, name=body.get("name"), color=color, clear_color=clear_color
    )
    if not found:
        raise HTTPException(status_code=404, detail="Tag not found")
    await audit(request, "tag.update", detail=str(tag_id), user=_user)
    return JSONResponse({"id": tag_id, "updated": True})


@router.delete("/api/tags/{tag_id}", status_code=204)
async def api_delete_tag(
    request: Request,
    tag_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Admin only. Cascades through entity_tags via ON DELETE CASCADE."""
    storage = get_storage(request)
    found = await storage.delete_tag(tag_id)
    if not found:
        raise HTTPException(status_code=404, detail="Tag not found")
    await audit(request, "tag.delete", detail=str(tag_id), user=_user)


@router.post("/api/tags/{source_id}/merge-into/{target_id}", status_code=200)
async def api_merge_tags(
    request: Request,
    source_id: int,
    target_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Admin only. Source absorbed into target; source is deleted."""
    storage = get_storage(request)
    try:
        await storage.merge_tags(source_id, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit(
        request,
        "tag.merge",
        detail=f"source={source_id} target={target_id}",
        user=_user,
    )
    return JSONResponse({"source_id": source_id, "target_id": target_id, "merged": True})


# ---------------------------------------------------------------------------
# /api/entities/{entity_type}/{entity_id}/tags — polymorphic attach/detach
# ---------------------------------------------------------------------------


def _validate_entity_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown entity_type {entity_type!r} (allowed: {sorted(ENTITY_TYPES)})",
        )


@router.get("/api/entities/{entity_type}/{entity_id}/tags")
async def api_list_entity_tags(
    request: Request,
    entity_type: str,
    entity_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    _validate_entity_type(entity_type)
    storage = get_storage(request)
    tags = await storage.list_tags_for_entity(entity_type, entity_id)
    return JSONResponse(tags)


@router.post("/api/entities/{entity_type}/{entity_id}/tags", status_code=201)
async def api_attach_entity_tag(
    request: Request,
    entity_type: str,
    entity_id: int,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Attach a tag. Body: {tag_id} OR {name} for inline-create-and-attach."""
    _validate_entity_type(entity_type)
    storage = get_storage(request)
    tag_id = body.get("tag_id")
    name = body.get("name")
    if tag_id is None and not name:
        raise HTTPException(status_code=422, detail="tag_id or name is required")
    if tag_id is None:
        assert isinstance(name, str)
        tag_id = await storage.get_or_create_tag(name.strip().lower())
    await storage.attach_tag(entity_type, entity_id, int(tag_id), user_id=user.get("id"))
    await audit(
        request,
        "tag.attach",
        detail=f"{entity_type}={entity_id} tag={tag_id}",
        user=user,
    )
    return JSONResponse(
        {"entity_type": entity_type, "entity_id": entity_id, "tag_id": tag_id},
        status_code=201,
    )


@router.delete("/api/entities/{entity_type}/{entity_id}/tags/{tag_id}", status_code=204)
async def api_detach_entity_tag(
    request: Request,
    entity_type: str,
    entity_id: int,
    tag_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    _validate_entity_type(entity_type)
    storage = get_storage(request)
    await storage.detach_tag(entity_type, entity_id, tag_id)
    await audit(
        request,
        "tag.detach",
        detail=f"{entity_type}={entity_id} tag={tag_id}",
        user=user,
    )


# ---------------------------------------------------------------------------
# Back-compat shims — legacy per-entity routes delegate to the generic path.
# Kept so the existing UI keeps working mid-rollout; removable once all
# callers migrate to /api/entities/.../tags.
# ---------------------------------------------------------------------------


@router.post("/api/sessions/{session_id}/tags", status_code=201)
async def api_add_session_tag(
    request: Request,
    session_id: int,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tag_id = body.get("tag_id")
    tag_name = body.get("tag_name") or body.get("name")
    if tag_id is None and not tag_name:
        raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
    if tag_id is None:
        assert isinstance(tag_name, str)
        tag_id = await storage.get_or_create_tag(tag_name)
    await storage.attach_tag("session", session_id, int(tag_id), user_id=user.get("id"))
    await audit(request, "session.tag.add", detail=f"session={session_id} tag={tag_id}", user=user)
    return JSONResponse({"session_id": session_id, "tag_id": tag_id}, status_code=201)


@router.delete("/api/sessions/{session_id}/tags/{tag_id}", status_code=204)
async def api_remove_session_tag(
    request: Request,
    session_id: int,
    tag_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.detach_tag("session", session_id, tag_id)
    await audit(
        request,
        "session.tag.remove",
        detail=f"session={session_id} tag={tag_id}",
        user=user,
    )


@router.get("/api/sessions/{session_id}/tags")
async def api_get_session_tags(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tags = await storage.list_tags_for_entity("session", session_id)
    return JSONResponse(tags)


@router.post("/api/notes/{note_id}/tags", status_code=201)
async def api_add_note_tag(
    request: Request,
    note_id: int,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tag_id = body.get("tag_id")
    tag_name = body.get("tag_name") or body.get("name")
    if tag_id is None and not tag_name:
        raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
    if tag_id is None:
        assert isinstance(tag_name, str)
        tag_id = await storage.get_or_create_tag(tag_name)
    await storage.attach_tag("session_note", note_id, int(tag_id), user_id=user.get("id"))
    await audit(request, "note.tag.add", detail=f"note={note_id} tag={tag_id}", user=user)
    return JSONResponse({"note_id": note_id, "tag_id": tag_id}, status_code=201)


@router.delete("/api/notes/{note_id}/tags/{tag_id}", status_code=204)
async def api_remove_note_tag(
    request: Request,
    note_id: int,
    tag_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.detach_tag("session_note", note_id, tag_id)
    await audit(
        request,
        "note.tag.remove",
        detail=f"note={note_id} tag={tag_id}",
        user=user,
    )


@router.get("/api/notes/{note_id}/tags")
async def api_get_note_tags(
    request: Request,
    note_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tags = await storage.list_tags_for_entity("session_note", note_id)
    return JSONResponse(tags)
