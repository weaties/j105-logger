"""Route handlers for settings."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import SETTINGS_BY_KEY, SETTINGS_DEFS, audit, get_storage

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return all curated settings with effective value and source."""
    storage = get_storage(request)

    db_settings = {r["key"]: r["value"] for r in await storage.list_settings()}
    result: list[dict[str, object]] = []
    for s in SETTINGS_DEFS:
        db_val = db_settings.get(s.key)
        env_val = os.environ.get(s.key)
        if db_val is not None:
            source, effective = "db", db_val
        elif env_val is not None:
            source, effective = "env", env_val
        else:
            source, effective = "default", s.default
        display = "••••••••" if s.sensitive and effective else effective
        result.append(
            {
                "key": s.key,
                "label": s.label,
                "input_type": s.input_type,
                "default_value": s.default,
                "help_text": s.help_text,
                "options": list(s.options),
                "sensitive": s.sensitive,
                "effective_value": display,
                "source": source,
            }
        )
    return JSONResponse({"settings": result})


@router.put("/api/settings")
async def api_put_settings(
    request: Request,
    body: dict[str, str],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Upsert settings. Empty string deletes the override (reverts to env/default)."""
    storage = get_storage(request)
    changed: list[str] = []
    for key, value in body.items():
        defn = SETTINGS_BY_KEY.get(key)
        if defn is None:
            raise HTTPException(status_code=422, detail=f"Unknown setting: {key}")
        value = str(value).strip()
        if value == "":
            # Delete override → revert to env/default
            await storage.delete_setting(key)
            # Remove from os.environ only if it came from our DB seeding
            # (don't remove actual shell env vars)
        else:
            await storage.set_setting(key, value)
            os.environ[key] = value
        changed.append(key)
    if changed:
        await audit(
            request,
            "settings.update",
            detail=", ".join(changed),
            user=_user,
        )
    return JSONResponse({"updated": changed})


@router.get("/api/color-schemes")
async def api_list_color_schemes(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return all available color schemes (presets + custom)."""
    storage = get_storage(request)
    from helmlog.themes import PRESET_ORDER, PRESETS

    presets = [
        {"id": pid, "name": PRESETS[pid].name, "type": "preset"}
        for pid in PRESET_ORDER
        if pid in PRESETS
    ]
    custom = [
        {**cs, "type": "custom", "id": f"custom:{cs['id']}"}
        for cs in await storage.list_color_schemes()
    ]
    boat_default = await storage.get_setting("color_scheme_default") or ""
    return JSONResponse({"presets": presets, "custom": custom, "boat_default": boat_default})


@router.post("/api/color-schemes", status_code=201)
async def api_create_color_scheme(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Create a new custom color scheme (admin only)."""
    storage = get_storage(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    bg = str(body.get("bg", "")).strip()
    text_color = str(body.get("text_color", "")).strip()
    accent = str(body.get("accent", "")).strip()
    if not all([name, bg, text_color, accent]):
        raise HTTPException(422, detail="name, bg, text_color, accent are required")
    scheme_id = await storage.create_color_scheme(name, bg, text_color, accent, _user.get("id"))
    await audit(request, "color_scheme.create", detail=f"name={name!r}", user=_user)
    return JSONResponse({"id": scheme_id, "name": name}, status_code=201)


# NOTE: /default must be registered before /{scheme_id} to avoid route shadowing.
@router.put("/api/color-schemes/default")
async def api_set_color_scheme_default(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Set the boat-wide default color scheme (admin only). Body: {scheme_id: str}."""
    storage = get_storage(request)
    body = await request.json()
    scheme = str(body.get("scheme_id", "")).strip()
    if not scheme:
        # Clear the default
        await storage.delete_setting("color_scheme_default")
        await audit(request, "color_scheme.default.clear", user=_user)
        return JSONResponse({"ok": True})
    # Validate the scheme exists
    from helmlog.themes import PRESETS

    if not scheme.startswith("custom:") and scheme not in PRESETS:
        raise HTTPException(422, detail=f"Unknown scheme: {scheme!r}")
    if scheme.startswith("custom:"):
        cs_id_str = scheme.removeprefix("custom:")
        if not cs_id_str.isdigit():
            raise HTTPException(422, detail="Invalid custom scheme id")
        cs = await storage.get_color_scheme(int(cs_id_str))
        if cs is None:
            raise HTTPException(404, detail="Custom color scheme not found")
    await storage.set_setting("color_scheme_default", scheme)
    await audit(request, "color_scheme.default.set", detail=f"scheme={scheme!r}", user=_user)
    return JSONResponse({"ok": True})


@router.put("/api/color-schemes/{scheme_id}")
async def api_update_color_scheme(
    request: Request,
    scheme_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update a custom color scheme (admin only)."""
    storage = get_storage(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    bg = str(body.get("bg", "")).strip()
    text_color = str(body.get("text_color", "")).strip()
    accent = str(body.get("accent", "")).strip()
    if not all([name, bg, text_color, accent]):
        raise HTTPException(422, detail="name, bg, text_color, accent are required")
    ok = await storage.update_color_scheme(scheme_id, name, bg, text_color, accent)
    if not ok:
        raise HTTPException(404, detail="Color scheme not found")
    await audit(request, "color_scheme.update", detail=f"id={scheme_id} name={name!r}", user=_user)
    return JSONResponse({"id": scheme_id, "name": name})


@router.delete("/api/color-schemes/{scheme_id}", status_code=204)
async def api_delete_color_scheme(
    request: Request,
    scheme_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Delete a custom color scheme (admin only)."""
    storage = get_storage(request)
    ok = await storage.delete_color_scheme(scheme_id)
    if not ok:
        raise HTTPException(404, detail="Color scheme not found")
    # If the deleted scheme was the boat default, clear it
    boat_default = await storage.get_setting("color_scheme_default")
    if boat_default == f"custom:{scheme_id}":
        await storage.delete_setting("color_scheme_default")
    await audit(request, "color_scheme.delete", detail=f"id={scheme_id}", user=_user)


@router.patch("/api/me/color-scheme")
async def api_set_my_color_scheme(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Set the calling user's personal color scheme override. Body: {scheme_id: str}."""
    storage = get_storage(request)
    body = await request.json()
    scheme = str(body.get("scheme_id") or "").strip() or None
    user_id = _user.get("id")
    if user_id is None:
        raise HTTPException(400, detail="Cannot set scheme for unauthenticated user")
    if scheme is not None:
        from helmlog.themes import PRESETS

        if not scheme.startswith("custom:") and scheme not in PRESETS:
            raise HTTPException(422, detail=f"Unknown scheme: {scheme!r}")
        if scheme.startswith("custom:"):
            cs_id_str = scheme.removeprefix("custom:")
            if not cs_id_str.isdigit():
                raise HTTPException(422, detail="Invalid custom scheme id")
            cs = await storage.get_color_scheme(int(cs_id_str))
            if cs is None:
                raise HTTPException(404, detail="Custom color scheme not found")
    await storage.set_user_color_scheme(user_id, scheme)
    return JSONResponse({"ok": True})


@router.delete("/api/me/color-scheme", status_code=204)
async def api_reset_my_color_scheme(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    """Reset the calling user's color scheme to the boat default."""
    storage = get_storage(request)
    user_id = _user.get("id")
    if user_id is None:
        raise HTTPException(400, detail="Cannot reset scheme for unauthenticated user")
    await storage.set_user_color_scheme(user_id, None)
