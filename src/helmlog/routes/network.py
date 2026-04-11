"""Route handlers for network."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/network/status")
async def api_network_status(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return WLAN status, interface list, internet, Tailscale, and Cloudflare."""
    get_storage(request)
    import helmlog.network as net_mod

    # Kick off all probes concurrently. We await each task individually so
    # mypy keeps the precise per-call return type instead of collapsing the
    # asyncio.gather result into a union of all return types (the overloads
    # stop at 6 awaitables, and we have 7).
    wlan_task = asyncio.create_task(net_mod.get_wlan_status())
    interfaces_task = asyncio.create_task(net_mod.list_interfaces())
    internet_task = asyncio.create_task(net_mod.check_internet())
    tailscale_task = asyncio.create_task(net_mod.get_tailscale_status())
    cloudflare_task = asyncio.create_task(net_mod.get_cloudflare_status())
    default_route_task = asyncio.create_task(net_mod.get_default_route())
    dns_task = asyncio.create_task(net_mod.check_dns())

    wlan_status = await wlan_task
    interfaces = await interfaces_task
    internet = await internet_task
    tailscale = await tailscale_task
    cloudflare = await cloudflare_task
    default_route = await default_route_task
    dns = await dns_task
    return JSONResponse(
        {
            "wlan": {
                "connected": wlan_status.connected,
                "ssid": wlan_status.ssid,
                "ip_address": wlan_status.ip_address,
                "signal_strength": wlan_status.signal_strength,
            },
            "interfaces": [
                {
                    "name": i.name,
                    "state": i.state,
                    "ip_address": i.ip_address,
                    "mac_address": i.mac_address,
                }
                for i in interfaces
            ],
            "internet": internet,
            "dns": dns,
            "default_route": {
                "interface": default_route.interface,
                "gateway": default_route.gateway,
            }
            if default_route
            else None,
            "tailscale": {
                "running": tailscale.running,
                "hostname": tailscale.hostname,
                "ip": tailscale.ip,
                "tailnet": tailscale.tailnet,
                "version": tailscale.version,
                "error": tailscale.error,
                "peers": [
                    {
                        "hostname": p.hostname,
                        "ip": p.ip,
                        "os": p.os,
                        "online": p.online,
                        "relay": p.relay,
                        "exit_node": p.exit_node,
                    }
                    for p in tailscale.peers
                ],
            },
            "cloudflare": {
                "installed": cloudflare.installed,
                "running": cloudflare.running,
                "hostname": cloudflare.hostname,
                "version": cloudflare.version,
                "error": cloudflare.error,
            },
        }
    )


@router.get("/api/network/profiles")
async def api_list_network_profiles(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List all WLAN profiles (camera + non-camera)."""
    storage = get_storage(request)
    # Camera networks from cameras table
    camera_rows = await storage.list_cameras()
    camera_profiles = [
        {
            "id": f"camera:{r['name']}",
            "name": f"{r['name']} — {r['wifi_ssid']}",
            "ssid": r["wifi_ssid"],
            "source": "camera",
            "is_default": False,
        }
        for r in camera_rows
        if r.get("wifi_ssid")
    ]
    # Non-camera profiles from wlan_profiles table
    wlan_rows = await storage.list_wlan_profiles()
    wlan_profiles = [
        {
            "id": f"profile:{r['id']}",
            "name": r["name"],
            "ssid": r["ssid"],
            "source": "saved",
            "is_default": bool(r["is_default"]),
        }
        for r in wlan_rows
    ]
    return JSONResponse(camera_profiles + wlan_profiles)


@router.post("/api/network/profiles", status_code=201)
async def api_add_network_profile(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Add a non-camera WLAN profile."""
    storage = get_storage(request)
    body = await request.json()
    name = body.get("name", "").strip()
    ssid = body.get("ssid", "").strip()
    password = body.get("password", "").strip() or None
    is_default = bool(body.get("is_default", False))
    if not name or not ssid:
        raise HTTPException(422, detail="name and ssid are required")
    pid = await storage.add_wlan_profile(name, ssid, password, is_default)
    await audit(request, "network.profile.add", detail=name, user=_user)
    return JSONResponse({"id": pid, "name": name, "ssid": ssid}, status_code=201)


@router.put("/api/network/profiles/{profile_id}")
async def api_update_network_profile(
    request: Request,
    profile_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update a non-camera WLAN profile."""
    storage = get_storage(request)
    body = await request.json()
    name = body.get("name", "").strip()
    ssid = body.get("ssid", "").strip()
    password = body.get("password", "").strip() or None
    is_default = bool(body.get("is_default", False))
    if not name or not ssid:
        raise HTTPException(422, detail="name and ssid are required")
    ok = await storage.update_wlan_profile(profile_id, name, ssid, password, is_default)
    if not ok:
        raise HTTPException(404, detail="Profile not found")
    await audit(request, "network.profile.update", detail=name, user=_user)
    return JSONResponse({"id": profile_id, "name": name, "ssid": ssid})


@router.delete("/api/network/profiles/{profile_id}", status_code=204)
async def api_delete_network_profile(
    request: Request,
    profile_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Delete a non-camera WLAN profile."""
    storage = get_storage(request)
    ok = await storage.delete_wlan_profile(profile_id)
    if not ok:
        raise HTTPException(404, detail="Profile not found")
    await audit(request, "network.profile.delete", detail=str(profile_id), user=_user)


@router.post("/api/network/connect")
async def api_network_connect(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Connect to a WLAN profile (camera or saved)."""
    storage = get_storage(request)
    import helmlog.network as net_mod

    body = await request.json()
    profile_id = body.get("profile_id", "")

    if str(profile_id).startswith("camera:"):
        camera_name = str(profile_id).removeprefix("camera:")
        cams = await storage.list_cameras()
        cam = next((c for c in cams if c["name"] == camera_name), None)
        if not cam or not cam.get("wifi_ssid"):
            raise HTTPException(404, detail="Camera network not found")
        result = await net_mod.connect_to_ssid(cam["wifi_ssid"], cam.get("wifi_password"))
    elif str(profile_id).startswith("profile:"):
        pid = int(str(profile_id).removeprefix("profile:"))
        profile = await storage.get_wlan_profile(pid)
        if not profile:
            raise HTTPException(404, detail="Profile not found")
        result = await net_mod.connect_to_ssid(profile["ssid"], profile.get("password"))
    else:
        raise HTTPException(422, detail="Invalid profile_id format")

    await audit(request, "network.connect", detail=str(profile_id), user=_user)
    return JSONResponse(
        {
            "success": result.success,
            "ssid": result.ssid,
            "error": result.error,
        }
    )


@router.post("/api/network/disconnect")
async def api_network_disconnect(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Disconnect WLAN (Ethernet-only mode)."""
    get_storage(request)
    import helmlog.network as net_mod

    result = await net_mod.disconnect_wlan()
    await audit(request, "network.disconnect", user=_user)
    return JSONResponse({"success": result.success, "error": result.error})
