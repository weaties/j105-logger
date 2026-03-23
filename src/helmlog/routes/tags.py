"""Route handlers for tags."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/tags")
async def api_list_tags(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tags = await storage.list_tags()
    return JSONResponse(tags)


@router.post("/api/tags", status_code=201)
async def api_create_tag(
    request: Request,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
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
    storage = get_storage(request)
    found = await storage.update_tag(tag_id, name=body.get("name"), color=body.get("color"))
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
    storage = get_storage(request)
    found = await storage.delete_tag(tag_id)
    if not found:
        raise HTTPException(status_code=404, detail="Tag not found")
    await audit(request, "tag.delete", detail=str(tag_id), user=_user)


@router.post("/api/sessions/{session_id}/tags", status_code=201)
async def api_add_session_tag(
    request: Request,
    session_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tag_id = body.get("tag_id")
    tag_name = body.get("tag_name")
    if tag_id is None and not tag_name:
        raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
    if tag_id is None:
        assert isinstance(tag_name, str)
        tag_id = await storage.get_or_create_tag(tag_name)
    await storage.add_session_tag(session_id, tag_id)
    await audit(request, "session.tag.add", detail=f"session={session_id} tag={tag_id}", user=_user)
    return JSONResponse({"session_id": session_id, "tag_id": tag_id}, status_code=201)


@router.delete("/api/sessions/{session_id}/tags/{tag_id}", status_code=204)
async def api_remove_session_tag(
    request: Request,
    session_id: int,
    tag_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.remove_session_tag(session_id, tag_id)
    await audit(
        request, "session.tag.remove", detail=f"session={session_id} tag={tag_id}", user=_user
    )


@router.get("/api/sessions/{session_id}/tags")
async def api_get_session_tags(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tags = await storage.get_session_tags(session_id)
    return JSONResponse(tags)


@router.post("/api/notes/{note_id}/tags", status_code=201)
async def api_add_note_tag(
    request: Request,
    note_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tag_id = body.get("tag_id")
    tag_name = body.get("tag_name")
    if tag_id is None and not tag_name:
        raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
    if tag_id is None:
        assert isinstance(tag_name, str)
        tag_id = await storage.get_or_create_tag(tag_name)
    await storage.add_note_tag(note_id, tag_id)
    await audit(request, "note.tag.add", detail=f"note={note_id} tag={tag_id}", user=_user)
    return JSONResponse({"note_id": note_id, "tag_id": tag_id}, status_code=201)


@router.delete("/api/notes/{note_id}/tags/{tag_id}", status_code=204)
async def api_remove_note_tag(
    request: Request,
    note_id: int,
    tag_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.remove_note_tag(note_id, tag_id)
    await audit(request, "note.tag.remove", detail=f"note={note_id} tag={tag_id}", user=_user)


@router.get("/api/notes/{note_id}/tags")
async def api_get_note_tags(
    request: Request,
    note_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    tags = await storage.get_note_tags(note_id)
    return JSONResponse(tags)
