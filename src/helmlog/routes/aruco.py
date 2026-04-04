"""Route handlers for ArUco marker tracking."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------


@router.get("/admin/aruco", response_class=HTMLResponse, include_in_schema=False)
async def admin_aruco_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(request, "admin/aruco.html", tpl_ctx(request, "/admin/aruco"))


# ---------------------------------------------------------------------------
# Cameras CRUD
# ---------------------------------------------------------------------------


@router.get("/api/aruco/cameras")
async def api_list_cameras(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List all ArUco cameras."""
    storage = get_storage(request)
    cameras = await storage.list_aruco_cameras()
    for cam in cameras:
        if cam.get("calibration"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                cam["calibration"] = json.loads(cam["calibration"])
    return JSONResponse(cameras)


@router.post("/api/aruco/cameras")
async def api_add_camera(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Add a new ArUco camera."""
    storage = get_storage(request)
    body = await request.json()
    name = body.get("name", "").strip()
    ip = body.get("ip", "").strip()
    if not name or not ip:
        raise HTTPException(400, "name and ip are required")
    marker_size_mm = float(body.get("marker_size_mm", 50.0))
    capture_interval_s = int(body.get("capture_interval_s", 60))
    retain_images = bool(body.get("retain_images", False))
    camera_id = await storage.add_aruco_camera(
        name, ip, marker_size_mm, capture_interval_s, retain_images
    )
    await audit(request, "aruco_camera_add", f"name={name} ip={ip}", _user)
    return JSONResponse({"id": camera_id}, status_code=201)


@router.put("/api/aruco/cameras/{camera_id}")
async def api_update_camera(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update an ArUco camera."""
    storage = get_storage(request)
    body = await request.json()
    ok = await storage.update_aruco_camera(
        camera_id,
        name=body.get("name"),
        ip=body.get("ip"),
        marker_size_mm=body.get("marker_size_mm"),
        capture_interval_s=body.get("capture_interval_s"),
        retain_images=body.get("retain_images"),
    )
    if not ok:
        raise HTTPException(404, "Camera not found")
    await audit(request, "aruco_camera_update", f"id={camera_id}", _user)
    return JSONResponse({"ok": True})


@router.delete("/api/aruco/cameras/{camera_id}")
async def api_delete_camera(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Delete an ArUco camera."""
    storage = get_storage(request)
    ok = await storage.delete_aruco_camera(camera_id)
    if not ok:
        raise HTTPException(404, "Camera not found")
    await audit(request, "aruco_camera_delete", f"id={camera_id}", _user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Camera profiles
# ---------------------------------------------------------------------------


@router.get("/api/aruco/cameras/{camera_id}/profiles")
async def api_list_profiles(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List profiles for a camera."""
    storage = get_storage(request)
    profiles = await storage.list_aruco_profiles(camera_id)
    for p in profiles:
        if p.get("settings"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                p["settings"] = json.loads(p["settings"])
    return JSONResponse(profiles)


@router.post("/api/aruco/cameras/{camera_id}/profiles")
async def api_add_profile(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Add a camera profile."""
    storage = get_storage(request)
    body = await request.json()
    name = body.get("name", "").strip()
    settings = body.get("settings", {})
    if not name:
        raise HTTPException(400, "name is required")
    profile_id = await storage.add_aruco_profile(
        camera_id, name, json.dumps(settings), is_active=bool(body.get("is_active", False))
    )
    return JSONResponse({"id": profile_id}, status_code=201)


@router.post("/api/aruco/profiles/{profile_id}/activate")
async def api_activate_profile(
    request: Request,
    profile_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Activate a camera profile."""
    storage = get_storage(request)
    ok = await storage.activate_aruco_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return JSONResponse({"ok": True})


@router.delete("/api/aruco/profiles/{profile_id}")
async def api_delete_profile(
    request: Request,
    profile_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Delete a camera profile."""
    storage = get_storage(request)
    ok = await storage.delete_aruco_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Controls CRUD
# ---------------------------------------------------------------------------


@router.get("/api/aruco/controls")
async def api_list_aruco_controls(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List controls that have ArUco marker config."""
    storage = get_storage(request)
    controls = await storage.controls_with_aruco()
    return JSONResponse(controls)


# ---------------------------------------------------------------------------
# Image ingestion (ESP32-CAM → HelmLog)
# ---------------------------------------------------------------------------


@router.post("/api/aruco/cameras/{name}/image")
async def api_ingest_image(
    request: Request,
    name: str,
    image: UploadFile,
    metadata: str | None = None,
) -> JSONResponse:
    """Accept an image + metadata from an ESP32-CAM and run detection."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera_by_name(name)
    if not camera:
        raise HTTPException(404, f"Camera '{name}' not found")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "Empty image")

    from helmlog.aruco_detector import (
        CameraCalibration,
        decode_jpeg,
        detect_and_measure,
    )

    try:
        img = decode_jpeg(image_bytes)
    except ValueError:
        raise HTTPException(400, "Invalid image data")  # noqa: B904

    # Parse calibration if available
    calibration = None
    if camera["calibration"]:
        try:
            cal_data = camera["calibration"]
            if isinstance(cal_data, str):
                pass  # already a string
            else:
                cal_data = json.dumps(cal_data)
            calibration = CameraCalibration.from_json(cal_data)
        except (json.JSONDecodeError, KeyError):
            pass

    # Get controls with ArUco config for this camera (unified controls table)
    all_aruco = await storage.controls_with_aruco()
    camera_controls = [c for c in all_aruco if c["camera_id"] == camera["id"]]
    pairs = [(c["marker_id_a"], c["marker_id_b"]) for c in camera_controls]
    control_map = {(c["marker_id_a"], c["marker_id_b"]): c for c in camera_controls}

    result = detect_and_measure(img, pairs, camera["marker_size_mm"], calibration)

    # Record measurements to boat_settings with source="camera"
    from datetime import UTC, datetime

    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None
    now = datetime.now(UTC).isoformat()
    recorded = 0

    for dist in result.distances:
        ctrl = control_map.get((dist.marker_id_a, dist.marker_id_b))
        if not ctrl:
            continue

        # Check tolerance (global default from aruco_settings, overridable per control)
        tolerance = await storage.get_aruco_tolerance_mm()
        if ctrl.get("tolerance_mm") is not None:
            tolerance = float(ctrl["tolerance_mm"])

        # Check against latest camera reading in boat_settings
        latest = await storage.get_latest_camera_reading(ctrl["name"])
        if latest is not None:
            delta_mm = abs(dist.distance_cm * 10 - float(latest["value"]) * 10)
            if delta_mm <= tolerance:
                continue

        # Save image if retention is enabled
        if camera["retain_images"]:
            import os

            ts_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            img_dir = os.path.join("data", "aruco", name)
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{ts_str}.jpg")
            with open(img_path, "wb") as f:
                f.write(image_bytes)

        # Write to boat_settings timeline
        await storage.create_boat_settings(
            race_id,
            [{"ts": now, "parameter": ctrl["name"], "value": str(dist.distance_cm)}],
            source="camera",
        )
        recorded += 1

    return JSONResponse(
        {
            "markers_detected": len(result.markers),
            "distances_measured": len(result.distances),
            "measurements_recorded": recorded,
        }
    )


# ---------------------------------------------------------------------------
# Live preview
# ---------------------------------------------------------------------------


@router.get("/api/aruco/cameras/{name}/preview")
async def api_camera_preview(
    request: Request,
    name: str,
) -> Response:
    """Return a thumbnail JPEG for a camera.

    First checks if the polling loop has a cached thumbnail. Falls back
    to fetching directly from the ESP32-CAM's /capture endpoint.
    """
    storage = get_storage(request)
    camera = await storage.get_aruco_camera_by_name(name)
    if not camera:
        raise HTTPException(404, f"Camera '{name}' not found")

    # Check for cached thumbnail from the polling loop
    thumbnails: dict[str, bytes] = getattr(storage, "_aruco_thumbnails", {})
    if name in thumbnails:
        return Response(content=thumbnails[name], media_type="image/jpeg")

    # Fall back to direct fetch from the camera
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://{camera['ip']}/capture")
            if resp.status_code != 200:
                raise HTTPException(502, "Camera returned an error")
            from helmlog.aruco_detector import create_thumbnail

            thumb = create_thumbnail(resp.content)
            return Response(content=thumb, media_type="image/jpeg")
    except httpx.HTTPError:
        raise HTTPException(502, "Camera unreachable")  # noqa: B904


# ---------------------------------------------------------------------------
# Camera settings proxy
# ---------------------------------------------------------------------------


@router.get("/api/aruco/cameras/{name}/settings")
async def api_camera_settings(
    request: Request,
    name: str,
    _user: dict[str, Any] = Depends(require_auth()),  # noqa: B008
) -> JSONResponse:
    """Proxy the ESP32-CAM's /settings endpoint to show sensor metadata."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera_by_name(name)
    if not camera:
        raise HTTPException(404, f"Camera '{name}' not found")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://{camera['ip']}/settings")
            if resp.status_code != 200:
                raise HTTPException(502, "Camera returned an error")
            return JSONResponse(resp.json())
    except httpx.HTTPError:
        raise HTTPException(502, "Camera unreachable")  # noqa: B904


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


@router.get("/api/aruco/controls/latest")
async def api_latest_camera_readings(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth()),  # noqa: B008
) -> JSONResponse:
    """Return the latest camera measurement for controls with ArUco config."""
    storage = get_storage(request)
    aruco_controls = await storage.controls_with_aruco()
    result: list[dict[str, Any]] = []
    for ctrl in aruco_controls:
        latest = await storage.get_latest_camera_reading(ctrl["name"])
        result.append(
            {
                "control_name": ctrl["name"],
                "camera_name": ctrl.get("camera_name", ""),
                "marker_id_a": ctrl["marker_id_a"],
                "marker_id_b": ctrl["marker_id_b"],
                "latest": latest,
            }
        )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@router.post("/api/aruco/cameras/{camera_id}/calibration/start")
async def api_start_calibration(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Begin calibration mode for a camera."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")
    await storage.update_aruco_calibration(camera_id, "", "capturing")
    await audit(request, "aruco_calibration_start", f"camera={camera['name']}", _user)
    return JSONResponse({"status": "capturing"})


@router.post("/api/aruco/cameras/{camera_id}/calibration/frame")
async def api_calibration_frame(
    request: Request,
    camera_id: int,
    image: UploadFile,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Submit a calibration frame. Returns frame count and whether ready to calibrate."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")
    if camera["calibration_state"] != "capturing":
        raise HTTPException(400, "Camera is not in calibration mode")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "Empty image")

    from helmlog.aruco_detector import CalibrationSession, decode_jpeg

    try:
        img = decode_jpeg(image_bytes)
    except ValueError:
        raise HTTPException(400, "Invalid image data")  # noqa: B904

    # Get or create session from app state
    cal_sessions: dict[int, CalibrationSession] = getattr(
        request.app.state, "aruco_cal_sessions", {}
    )
    if not hasattr(request.app.state, "aruco_cal_sessions"):
        request.app.state.aruco_cal_sessions = cal_sessions

    if camera_id not in cal_sessions:
        cal_sessions[camera_id] = CalibrationSession()

    session = cal_sessions[camera_id]
    found = session.add_frame(img)

    return JSONResponse(
        {
            "corners_found": found,
            "frame_count": session.frame_count,
            "required_frames": session.required_frames,
            "ready": session.is_ready,
        }
    )


@router.post("/api/aruco/cameras/{camera_id}/calibration/run")
async def api_run_calibration(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Run calibration from collected frames."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")

    cal_sessions: dict[int, Any] = getattr(request.app.state, "aruco_cal_sessions", {})
    session = cal_sessions.get(camera_id)
    if not session or not session.is_ready:
        raise HTTPException(400, "Not enough calibration frames")

    result = session.calibrate()

    if result.reprojection_error > 2.0:
        state = "calibration_failed"
    elif result.reprojection_error > 1.0:
        state = "calibrated"  # acceptable with warning
    else:
        state = "calibrated"

    await storage.update_aruco_calibration(camera_id, result.to_json(), state)
    cal_sessions.pop(camera_id, None)

    await audit(
        request,
        "aruco_calibration_complete",
        f"camera={camera['name']} error={result.reprojection_error:.3f}px",
        _user,
    )

    return JSONResponse(
        {
            "state": state,
            "reprojection_error_px": round(result.reprojection_error, 4),
            "frame_count": result.frame_count,
        }
    )


@router.post("/api/aruco/cameras/{camera_id}/calibration/reset")
async def api_reset_calibration(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Reset calibration to uncalibrated."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")
    await storage.update_aruco_calibration(camera_id, "", "uncalibrated")
    cal_sessions: dict[int, Any] = getattr(request.app.state, "aruco_cal_sessions", {})
    cal_sessions.pop(camera_id, None)
    await audit(request, "aruco_calibration_reset", f"camera={camera['name']}", _user)
    return JSONResponse({"status": "uncalibrated"})

    # Trigger words and controls are now managed via the unified /api/controls endpoints.
    # See routes/controls.py.


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/api/aruco/settings")
async def api_get_settings(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return ArUco settings."""
    storage = get_storage(request)
    tolerance = await storage.get_aruco_setting("tolerance_mm_default")
    return JSONResponse(
        {
            "tolerance_mm_default": float(tolerance) if tolerance else 5.0,
        }
    )


@router.put("/api/aruco/settings")
async def api_update_settings(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update ArUco settings."""
    storage = get_storage(request)
    body = await request.json()
    if "tolerance_mm_default" in body:
        await storage.set_aruco_setting(
            "tolerance_mm_default", str(float(body["tolerance_mm_default"]))
        )
    await audit(request, "aruco_settings_update", None, _user)
    return JSONResponse({"ok": True})
