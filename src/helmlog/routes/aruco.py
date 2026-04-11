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


@router.post("/api/aruco/profiles/{profile_id}/load")
async def api_load_profile(
    request: Request,
    profile_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Load a profile's settings onto its camera (activate + push to ESP32-CAM)."""
    storage = get_storage(request)

    # Get profile and its camera
    profiles_db = storage._read_conn()
    cur = await profiles_db.execute(
        "SELECT p.id, p.camera_id, p.name, p.settings, c.ip"
        " FROM aruco_camera_profiles p"
        " JOIN aruco_cameras c ON c.id = p.camera_id"
        " WHERE p.id = ?",
        (profile_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Profile not found")

    settings = json.loads(row["settings"]) if row["settings"] else {}

    # Activate this profile in DB
    await storage.activate_aruco_profile(profile_id)

    # Push settings to the ESP32-CAM
    if settings:
        import httpx

        form_data = "&".join(f"{k}={v}" for k, v in settings.items())
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"http://{row['ip']}/settings",
                    content=form_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError:
            pass  # Camera may be offline — settings saved in DB regardless

    await audit(request, "aruco_profile_load", f"profile={row['name']}", _user)
    return JSONResponse({"ok": True, "settings": settings})


@router.post("/api/aruco/cameras/{camera_id}/profiles/save-current")
async def api_save_current_profile(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Capture the camera's current sensor settings and save as a named profile."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    # Fetch current settings from ESP32-CAM
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://{camera['ip']}/settings")
            if resp.status_code != 200:
                raise HTTPException(502, "Camera returned an error")
            settings = resp.json()
    except httpx.HTTPError:
        raise HTTPException(502, "Camera unreachable")  # noqa: B904

    # Keep only the sensor-relevant keys
    keep = {
        "brightness",
        "contrast",
        "saturation",
        "sharpness",
        "quality",
        "framesize",
        "whitebal",
        "awb_gain",
        "wb_mode",
        "exposure_ctrl",
        "aec2",
        "ae_level",
        "aec_value",
        "gain_ctrl",
        "agc_gain",
        "gainceiling",
        "hmirror",
        "vflip",
        "denoise",
    }
    filtered = {k: v for k, v in settings.items() if k in keep}

    profile_id = await storage.add_aruco_profile(
        camera_id, name, json.dumps(filtered), is_active=True
    )
    await audit(request, "aruco_profile_save", f"name={name} camera={camera['name']}", _user)
    return JSONResponse({"id": profile_id, "settings": filtered}, status_code=201)


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


@router.post("/api/aruco/cameras/{name}/settings")
async def api_update_camera_settings(
    request: Request,
    name: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Push a setting change to the ESP32-CAM's /settings endpoint."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera_by_name(name)
    if not camera:
        raise HTTPException(404, f"Camera '{name}' not found")

    body = await request.json()
    # Build form-encoded body matching the ESP32-CAM's POST /settings format
    form_data = "&".join(f"{k}={v}" for k, v in body.items())

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"http://{camera['ip']}/settings",
                content=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
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


@router.get("/api/aruco/calibration/checkerboard.pdf")
async def api_calibration_checkerboard(
    request: Request,
    cols: int = 9,
    rows: int = 6,
    square_mm: int = 25,
) -> Response:
    """Generate a printable checkerboard PDF for camera calibration.

    Default: 9x6 inner corners, 25mm squares — suitable for A4/Letter paper.
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    # Render at 300 DPI
    dpi = 300
    mm_to_px = dpi / 25.4
    sq_px = int(square_mm * mm_to_px)

    # Board includes 1 extra row/col of squares on each side for the border
    board_cols = cols + 1
    board_rows = rows + 1
    board_w = board_cols * sq_px
    board_h = board_rows * sq_px

    # Page size: US Letter at 300 DPI (215.9x279.4mm)
    page_w = int(215.9 * mm_to_px)
    page_h = int(279.4 * mm_to_px)

    img = Image.new("L", (page_w, page_h), 255)
    draw = ImageDraw.Draw(img)

    # Center the board on the page
    x_off = (page_w - board_w) // 2
    y_off = (page_h - board_h) // 2 + int(10 * mm_to_px)  # shift down for title

    for r in range(board_rows):
        for c in range(board_cols):
            if (r + c) % 2 == 0:
                x0 = x_off + c * sq_px
                y0 = y_off + r * sq_px
                draw.rectangle([x0, y0, x0 + sq_px, y0 + sq_px], fill=0)

    # Title text
    title = f"HelmLog Calibration Target — {cols}x{rows} inner corners, {square_mm}mm squares"
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), title, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((page_w - tw) // 2, int(5 * mm_to_px)), title, fill=0, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=dpi)
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=calibration-checkerboard.pdf"},
    )


@router.post("/api/aruco/cameras/{camera_id}/calibration/start")
async def api_start_calibration(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Begin calibration mode for a camera.

    Accepts optional JSON body with checkerboard dimensions:
    ``{"cols": 9, "rows": 6, "square_mm": 25}``
    """
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")

    # Parse optional checkerboard config
    cols, rows, square_mm = 9, 6, 25.0
    try:
        body = await request.json()
        cols = int(body.get("cols", cols))
        rows = int(body.get("rows", rows))
        square_mm = float(body.get("square_mm", square_mm))
    except Exception:  # noqa: BLE001
        pass  # No body or invalid JSON — use defaults

    await storage.update_aruco_calibration(camera_id, "", "capturing")

    # Create calibration session with the specified dimensions
    from helmlog.aruco_detector import CalibrationSession

    cal_sessions: dict[int, CalibrationSession] = getattr(
        request.app.state, "aruco_cal_sessions", {}
    )
    if not hasattr(request.app.state, "aruco_cal_sessions"):
        request.app.state.aruco_cal_sessions = cal_sessions
    cal_sessions[camera_id] = CalibrationSession(cols=cols, rows=rows, square_mm=square_mm)

    await audit(request, "aruco_calibration_start", f"camera={camera['name']}", _user)
    return JSONResponse({"status": "capturing", "cols": cols, "rows": rows})


@router.post("/api/aruco/cameras/{camera_id}/calibration/capture")
async def api_calibration_capture(
    request: Request,
    camera_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Capture a frame from the ESP32-CAM and use it as a calibration frame."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")
    if camera["calibration_state"] != "capturing":
        raise HTTPException(400, "Camera is not in calibration mode")

    # Fetch frame from ESP32-CAM
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{camera['ip']}/capture")
            if resp.status_code != 200:
                raise HTTPException(502, "Camera capture failed")
            image_bytes = resp.content
    except httpx.HTTPError:
        raise HTTPException(502, "Camera unreachable")  # noqa: B904

    return _process_calibration_frame(request, camera_id, image_bytes)


@router.post("/api/aruco/cameras/{camera_id}/calibration/frame")
async def api_calibration_frame(
    request: Request,
    camera_id: int,
    image: UploadFile,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Submit a calibration frame (upload). Returns frame count and whether ready."""
    storage = get_storage(request)
    camera = await storage.get_aruco_camera(camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")
    if camera["calibration_state"] != "capturing":
        raise HTTPException(400, "Camera is not in calibration mode")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "Empty image")

    return _process_calibration_frame(request, camera_id, image_bytes)


def _process_calibration_frame(
    request: Request, camera_id: int, image_bytes: bytes
) -> JSONResponse:
    """Shared logic: decode image, find corners, add to calibration session."""

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
