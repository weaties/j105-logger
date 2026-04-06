"""Route handlers for device API key management (#423)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import generate_token, hash_api_key, require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()


@router.get("/admin/devices", response_class=HTMLResponse, include_in_schema=False)
async def admin_devices_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    return templates.TemplateResponse(
        request, "admin/devices.html", tpl_ctx(request, "/admin/devices")
    )


@router.get("/api/devices")
async def api_list_devices(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List all registered devices."""
    storage = get_storage(request)
    devices = await storage.list_devices()
    return JSONResponse(devices)


@router.post("/admin/devices")
async def admin_create_device(
    request: Request,
    name: str = Form(),
    role: str = Form(default="crew"),
    scope: str | None = Form(default=None),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Create a new device API key. Returns the plaintext key (shown once)."""
    if role not in ("crew", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be crew or viewer")

    storage = get_storage(request)
    api_key = generate_token(32)
    key_hash = hash_api_key(api_key)

    device_id = await storage.create_device(
        name=name,
        key_hash=key_hash,
        role=role,
        scope=scope if scope else None,
    )

    await audit(request, "devices.create", detail=name, user=_user)

    return JSONResponse(
        {
            "id": device_id,
            "name": name,
            "role": role,
            "api_key": api_key,
        }
    )


@router.delete("/admin/devices/{device_id}")
async def admin_revoke_device(
    request: Request,
    device_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Revoke (deactivate) a device."""
    storage = get_storage(request)
    device = await storage.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    await storage.revoke_device(device_id)
    await audit(request, "devices.revoke", detail=device["name"], user=_user)

    return JSONResponse({"ok": True})


@router.post("/admin/devices/{device_id}/rotate")
async def admin_rotate_device_key(
    request: Request,
    device_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Rotate a device's API key. Returns the new plaintext key (shown once)."""
    storage = get_storage(request)
    device = await storage.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    new_key = generate_token(32)
    new_hash = hash_api_key(new_key)
    await storage.rotate_device_key(device_id, new_hash)
    await audit(request, "devices.rotate", detail=device["name"], user=_user)

    return JSONResponse({"id": device_id, "name": device["name"], "api_key": new_key})
