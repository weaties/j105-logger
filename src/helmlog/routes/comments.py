"""Route handlers for comments."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.post("/api/sessions/{session_id}/threads", status_code=201)
async def api_create_thread(
    request: Request,
    session_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Create a comment thread for a session.

    Body: `{ "title": str?, "anchor": Anchor? }`. The Anchor schema is
    `{kind, entity_id?, t_start?, t_end?}` — see `helmlog.anchors`.
    Legacy keys `anchor_timestamp` / `mark_reference` return 400 (cutover
    landed in #478 / slice 2 of the Moments epic).
    """
    from helmlog.anchors import Anchor, AnchorError  # noqa: PLC0415
    from helmlog.storage import AnchorScopeError  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    if "anchor_timestamp" in body or "mark_reference" in body:
        raise HTTPException(
            status_code=400,
            detail=(
                "anchor_timestamp / mark_reference are no longer accepted; "
                "use the `anchor` object ({kind, entity_id?, t_start?, t_end?})."
            ),
        )
    title: str | None = body.get("title")
    anchor_payload = body.get("anchor")
    anchor: Anchor | None = None
    if anchor_payload is not None:
        if not isinstance(anchor_payload, dict):
            raise HTTPException(status_code=400, detail="anchor must be an object")
        try:
            anchor = Anchor.from_dict(anchor_payload)
        except AnchorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        thread_id = await storage.create_comment_thread(
            session_id, user["id"], anchor=anchor, title=title
        )
    except AnchorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AnchorScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit(
        request, "thread.create", detail=f"thread={thread_id} session={session_id}", user=user
    )
    # Notify (#284)
    from helmlog.notifications import notify_new_thread  # noqa: PLC0415

    await notify_new_thread(storage, thread_id, session_id, user["id"])
    return JSONResponse({"id": thread_id}, status_code=201)


@router.get("/api/sessions/{session_id}/anchors")
async def api_list_session_anchors(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return a pre-labelled, t_start-ordered list of pickable anchors.

    Consumed by the anchor-picker typeahead when composing a thread.
    """
    storage = get_storage(request)
    anchors = await storage.list_session_anchors(session_id)
    return JSONResponse(anchors)


@router.get("/api/sessions/{session_id}/threads")
async def api_list_threads(
    request: Request,
    session_id: int,
    tags: str | None = None,
    tag_mode: str = "and",
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List threads for a session with unread counts.

    Response: `{threads: [...], available_tags: [...]}`. Each thread gets
    a `tags` array; `available_tags` stays populated across the chip row
    even when a tag filter is active. `?tags=1,2&tag_mode=and|or` narrows
    the list.
    """
    storage = get_storage(request)
    threads = await storage.list_comment_threads(session_id, user["id"])
    thread_ids = [t["id"] for t in threads]
    tag_map = await storage.list_tags_for_entities("thread", thread_ids)
    for t in threads:
        t["tags"] = tag_map.get(t["id"], [])

    available_counts: dict[int, dict[str, Any]] = {}
    for t in threads:
        for tag in t.get("tags") or []:
            entry = available_counts.setdefault(
                tag["id"],
                {
                    "id": tag["id"],
                    "name": tag["name"],
                    "color": tag["color"],
                    "count": 0,
                },
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
                    await storage.list_entities_with_tags("thread", tag_ids, mode=tag_mode)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            threads = [t for t in threads if t["id"] in allowed]
    return JSONResponse({"threads": threads, "available_tags": available_tags})


@router.get("/api/threads/{thread_id}")
async def api_get_thread(
    request: Request,
    thread_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Get a thread with all its comments."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return JSONResponse(thread)


@router.post("/api/threads/{thread_id}/comments", status_code=201)
async def api_create_comment(
    request: Request,
    thread_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Add a comment to a thread."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    body = await request.json()
    text: str = body.get("body", "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="body is required")
    comment_id = await storage.create_comment(thread_id, user["id"], text)
    await audit(
        request, "comment.create", detail=f"comment={comment_id} thread={thread_id}", user=user
    )
    # Notifications (#284): parse mentions + notify
    from helmlog.notifications import (  # noqa: PLC0415
        notify_mention,
        notify_reply,
        parse_mentions,
    )

    session_id = thread["session_id"]
    all_users = await storage.list_users()
    known_names = [u["name"] for u in all_users if u.get("name")]
    mentioned_names = parse_mentions(text, known_names=known_names)
    if mentioned_names:
        name_map = await storage.resolve_user_names(mentioned_names)
        if name_map:
            await notify_mention(
                storage,
                comment_id,
                thread_id,
                session_id,
                user["id"],
                list(name_map.values()),
            )
    await notify_reply(storage, comment_id, thread_id, session_id, user["id"])
    return JSONResponse({"id": comment_id}, status_code=201)


@router.put("/api/comments/{comment_id}")
async def api_update_comment(
    request: Request,
    comment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Edit a comment. Only the author can edit."""
    storage = get_storage(request)
    comment = await storage.get_comment(comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment["author"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the author or admin can edit")
    body = await request.json()
    text: str = body.get("body", "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="body is required")
    await storage.update_comment(comment_id, text)
    await audit(request, "comment.update", detail=f"comment={comment_id}", user=user)
    return JSONResponse({"ok": True})


@router.delete("/api/comments/{comment_id}", status_code=204)
async def api_delete_comment(
    request: Request,
    comment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Delete a comment. Only the author or admin can delete."""
    storage = get_storage(request)
    comment = await storage.get_comment(comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment["author"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the author or admin can delete")
    await storage.delete_comment(comment_id)
    await audit(request, "comment.delete", detail=f"comment={comment_id}", user=user)
    return Response(status_code=204)


@router.post("/api/threads/{thread_id}/resolve")
async def api_resolve_thread(
    request: Request,
    thread_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Resolve a thread. Only the creator or admin can resolve."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["created_by"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the thread creator or admin can resolve")
    body = await request.json()
    summary: str | None = body.get("resolution_summary")
    await storage.resolve_comment_thread(thread_id, user["id"], summary)
    await audit(request, "thread.resolve", detail=f"thread={thread_id}", user=user)
    # Notify (#284)
    from helmlog.notifications import notify_resolved  # noqa: PLC0415

    await notify_resolved(storage, thread_id, thread["session_id"], user["id"])
    return JSONResponse({"ok": True})


@router.post("/api/threads/{thread_id}/unresolve")
async def api_unresolve_thread(
    request: Request,
    thread_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Unresolve a thread. Only the creator or admin can unresolve."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["created_by"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="Only the thread creator or admin can unresolve"
        )
    await storage.unresolve_comment_thread(thread_id)
    await audit(request, "thread.unresolve", detail=f"thread={thread_id}", user=user)
    return JSONResponse({"ok": True})


@router.post("/api/threads/{thread_id}/read")
async def api_mark_thread_read(
    request: Request,
    thread_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Mark a thread as read for the current user."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    await storage.mark_thread_read(thread_id, user["id"])
    return JSONResponse({"ok": True})


@router.delete("/api/threads/{thread_id}", status_code=204)
async def api_delete_thread(
    request: Request,
    thread_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Delete a thread. Only the creator or admin can delete."""
    storage = get_storage(request)
    thread = await storage.get_comment_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["created_by"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only the thread creator or admin can delete")
    await storage.delete_comment_thread(thread_id)
    await audit(request, "thread.delete", detail=f"thread={thread_id}", user=user)
    return Response(status_code=204)


@router.post("/api/comments/redact-author")
async def api_redact_comment_author(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Redact comment attribution for a user. User can redact self; admin can redact anyone."""
    storage = get_storage(request)
    body = await request.json()
    target_user_id: int = body.get("user_id", user["id"])
    if target_user_id != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admin can redact other users")
    count = await storage.redact_comment_author(target_user_id)
    # Cascade to notifications (#284)
    await storage.cascade_crew_redaction_to_notifications(target_user_id)
    await audit(request, "comment.redact", detail=f"user={target_user_id} count={count}", user=user)
    return JSONResponse({"redacted": count})


# ------------------------------------------------------------------
# /api/sails  &  /api/sessions/{id}/sails
# ------------------------------------------------------------------


_POINT_OF_SAIL_VALUES = ("upwind", "downwind", "both")
