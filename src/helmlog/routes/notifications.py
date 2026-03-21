"""Route handlers for notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import get_storage

router = APIRouter()


@router.get("/api/notifications")
async def api_notifications(
    request: Request,
    unread_only: bool = False,
    limit: int = 50,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return notifications for the current user."""
    storage = get_storage(request)
    notifs = await storage.get_notifications(user["id"], unread_only=unread_only, limit=limit)
    return JSONResponse(notifs)


@router.get("/api/notifications/count")
async def api_notification_count(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return unread + mention count for nav badge."""
    storage = get_storage(request)
    counts = await storage.get_notification_count(user["id"])
    return JSONResponse(counts)


@router.post("/api/notifications/{notification_id}/read")
async def api_mark_notification_read(
    request: Request,
    notification_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Mark one notification as read."""
    storage = get_storage(request)
    ok = await storage.mark_notification_read(notification_id, user["id"])
    if not ok:
        raise HTTPException(404, "Notification not found")
    return JSONResponse({"ok": True})


@router.post("/api/notifications/read-all")
async def api_mark_all_read(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Mark all notifications as read."""
    storage = get_storage(request)
    count = await storage.mark_all_notifications_read(user["id"])
    return JSONResponse({"marked": count})


@router.delete("/api/notifications/{notification_id}")
async def api_dismiss_notification(
    request: Request,
    notification_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Dismiss a notification."""
    storage = get_storage(request)
    ok = await storage.dismiss_notification(notification_id, user["id"])
    if not ok:
        raise HTTPException(404, "Notification not found")
    return Response(status_code=204)


@router.get("/api/notifications/preferences")
async def api_notification_preferences(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return notification preferences for the current user."""
    storage = get_storage(request)
    prefs = await storage.get_notification_preferences(user["id"])
    return JSONResponse(prefs)


@router.put("/api/notifications/preferences")
async def api_set_notification_preference(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Update a notification preference."""
    storage = get_storage(request)
    body = await request.json()
    scope: str = body.get("scope", "session")
    ntype: str = body.get("type", "")
    channel: str = body.get("channel", "platform")
    enabled: bool = body.get("enabled", True)
    frequency: str = body.get("frequency", "immediate")
    if not ntype:
        raise HTTPException(422, "type is required")
    await storage.set_notification_preference(
        user["id"],
        scope,
        ntype,
        channel,
        enabled=enabled,
        frequency=frequency,
    )
    return JSONResponse({"ok": True})
