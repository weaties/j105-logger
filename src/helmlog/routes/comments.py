"""Route handlers for comments on moments (#662, was threads under #282).

Comment create lives under ``/api/moments/{id}/comments`` (see
``routes/moments.py``). The standalone ``/api/comments/{id}`` endpoints
here cover edit / delete / author redaction — operations that don't need
the parent moment in the URL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.put("/api/comments/{comment_id}")
async def api_update_comment(
    request: Request,
    comment_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Edit a comment. Only the author (or admin) can edit."""
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


@router.post("/api/comments/redact-author")
async def api_redact_comment_author(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Redact comment attribution for a user. Any user may redact their
    own comments; only admin may redact another user's attribution."""
    storage = get_storage(request)
    body = await request.json()
    target_user_id: int = body.get("user_id", user["id"])
    if target_user_id != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admin can redact other users")
    count = await storage.redact_comment_author(target_user_id)
    await storage.cascade_crew_redaction_to_notifications(target_user_id)
    await audit(request, "comment.redact", detail=f"user={target_user_id} count={count}", user=user)
    return JSONResponse({"redacted": count})
