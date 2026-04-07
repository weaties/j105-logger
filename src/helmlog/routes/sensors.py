"""Route handlers for ESP32 string potentiometer sensors (#432)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, limiter
from helmlog.sensors import (
    DeviceState,
    ReadingRequest,
    compute_mm_per_adc,
    process_active_reading,
    process_reading,
    validate_mac,
)

router = APIRouter()


@router.post("/api/sensor/reading")
@limiter.limit("60/minute")
async def api_sensor_reading(request: Request) -> JSONResponse:
    """Accept a reading from an ESP32 sensor. No auth required."""
    body = await request.json()

    mac = body.get("mac", "")
    if not validate_mac(mac):
        raise HTTPException(status_code=422, detail="Invalid MAC format")

    raw_adc = body.get("raw_adc")
    if raw_adc is None or not isinstance(raw_adc, int):
        raise HTTPException(status_code=422, detail="raw_adc is required and must be an integer")

    reading_req = ReadingRequest(
        mac=mac,
        raw_adc=raw_adc,
        battery_mv=body.get("battery_mv"),
        rssi=body.get("rssi"),
        button_pressed=bool(body.get("button_pressed", False)),
        estimated_utc=body.get("estimated_utc"),
    )

    storage = get_storage(request)

    # Look up or register the device
    device = await storage.get_sensor_device(mac)
    if device is None:
        await storage.register_sensor_device(mac)
        device = await storage.get_sensor_device(mac)
        assert device is not None
        logger.info("New sensor device registered: {}", mac)

    # Get session state
    session_active = storage.session_active
    current_race = await storage.get_current_race() if session_active else None
    current_session_id = current_race.id if current_race else None

    # Get last stored reading for threshold comparison
    last_stored_mm: float | None = None
    if current_session_id is not None:
        last_stored_mm = await storage.get_last_sensor_reading_mm(mac, current_session_id)

    # Determine if device is in "active" mode (was promoted to active on a previous check-in)
    # The device state in DB is always 'ready' — "active" is a runtime state
    # driven by session_active. We track it via presence of readings in the current session.
    is_runtime_active = (
        device.state == DeviceState.READY
        and current_session_id is not None
        and last_stored_mm is not None
    )

    if is_runtime_active:
        response, reading, updates = process_active_reading(
            device, reading_req, session_active, current_session_id, last_stored_mm
        )
    else:
        response, reading, updates = process_reading(
            device, reading_req, session_active, current_session_id, last_stored_mm
        )

    # Apply device updates
    if updates:
        # Don't persist "active" as a DB state — it's runtime-only
        db_updates = {k: v for k, v in updates.items() if not (k == "state" and v == "active")}
        if db_updates:
            await storage.update_sensor_device(mac, db_updates)

    # Store reading if produced
    if reading is not None:
        await storage.store_sensor_reading(
            session_id=reading.session_id,
            mac=reading.mac,
            timestamp_utc=reading.timestamp_utc,
            raw_adc=reading.raw_adc,
            position_mm=reading.position_mm,
            battery_mv=reading.battery_mv,
        )

    # Log warnings for out-of-range ADC
    if raw_adc < 0 or raw_adc > 4095:
        logger.warning("Sensor {} reported out-of-range ADC: {}", mac, raw_adc)

    return JSONResponse(
        {
            "state": response.state,
            "sleep_seconds": response.sleep_seconds,
            "session_active": response.session_active,
            "server_time_utc": response.server_time_utc,
        }
    )


# ---------------------------------------------------------------------------
# Admin endpoints for sensor management
# ---------------------------------------------------------------------------


@router.get("/api/sensors")
async def api_list_sensors(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List all registered sensor devices."""
    storage = get_storage(request)
    devices = await storage.list_sensor_devices()
    return JSONResponse(
        [
            {
                "mac": d.mac,
                "line_name": d.line_name,
                "friendly_name": d.friendly_name,
                "state": d.state,
                "mm_per_adc_count": d.mm_per_adc_count,
                "total_travel_mm": d.total_travel_mm,
                "zero_offset_adc": d.zero_offset_adc,
                "last_zeroed_at": d.last_zeroed_at,
                "sleep_idle_s": d.sleep_idle_s,
                "sleep_active_s": d.sleep_active_s,
                "report_threshold_mm": d.report_threshold_mm,
                "last_seen_at": d.last_seen_at,
                "last_battery_mv": d.last_battery_mv,
                "last_rssi": d.last_rssi,
            }
            for d in devices
        ]
    )


@router.put("/api/sensors/{mac}")
async def api_update_sensor(
    request: Request,
    mac: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update sensor device configuration."""
    if not validate_mac(mac):
        raise HTTPException(status_code=422, detail="Invalid MAC format")

    storage = get_storage(request)
    device = await storage.get_sensor_device(mac)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    body = await request.json()
    updates: dict[str, object] = {}

    # Editable fields
    for field in (
        "line_name",
        "friendly_name",
        "pulley_diameter_mm",
        "cal_adc_a",
        "cal_adc_b",
        "cal_distance_mm",
        "total_travel_mm",
        "sleep_idle_s",
        "sleep_active_s",
        "report_threshold_mm",
    ):
        if field in body:
            updates[field] = body[field]

    # If line_name is being set on an unregistered device, transition to uncalibrated
    if "line_name" in updates and updates["line_name"] and device.state == DeviceState.UNREGISTERED:
        updates["state"] = DeviceState.UNCALIBRATED

    # If calibration params changed, recompute mm_per_adc_count
    cal_a_raw = updates.get("cal_adc_a", device.cal_adc_a)
    cal_b_raw = updates.get("cal_adc_b", device.cal_adc_b)
    cal_dist_raw = updates.get("cal_distance_mm", device.cal_distance_mm)

    if cal_a_raw is not None and cal_b_raw is not None and cal_dist_raw is not None:
        cal_a_int = int(str(cal_a_raw))
        cal_b_int = int(str(cal_b_raw))
        cal_dist_f = float(str(cal_dist_raw))
        mm_per_adc = compute_mm_per_adc(cal_a_int, cal_b_int, cal_dist_f)
        if mm_per_adc is not None:
            updates["mm_per_adc_count"] = mm_per_adc
            # If calibration just completed, transition to needs_zero
            if device.state == DeviceState.UNCALIBRATED:
                updates["state"] = DeviceState.NEEDS_ZERO
        # If calibration params were edited on a calibrated device, invalidate
        cal_fields_changed = any(
            f in updates for f in ("cal_adc_a", "cal_adc_b", "cal_distance_mm")
        )
        if cal_fields_changed and device.state in (DeviceState.NEEDS_ZERO, DeviceState.READY):
            updates["state"] = DeviceState.UNCALIBRATED
            updates["zero_offset_adc"] = None
            updates["last_zeroed_at"] = None

    if updates:
        await storage.update_sensor_device(mac, updates)
        await audit(request, "sensors.update", detail=mac, user=_user)

    updated = await storage.get_sensor_device(mac)
    assert updated is not None
    return JSONResponse({"mac": updated.mac, "state": updated.state})


@router.delete("/api/sensors/{mac}")
async def api_delete_sensor(
    request: Request,
    mac: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Delete a sensor device and all its readings."""
    if not validate_mac(mac):
        raise HTTPException(status_code=422, detail="Invalid MAC format")

    storage = get_storage(request)
    device = await storage.get_sensor_device(mac)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    await storage.delete_sensor_device(mac)
    await audit(request, "sensors.delete", detail=mac, user=_user)
    return JSONResponse({"ok": True})


@router.get("/api/sensors/{mac}/readings")
async def api_sensor_readings(
    request: Request,
    mac: str,
    session_id: int | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return sensor readings, optionally filtered by session."""
    if not validate_mac(mac):
        raise HTTPException(status_code=422, detail="Invalid MAC format")

    storage = get_storage(request)
    if session_id is None:
        # Get current session
        race = await storage.get_current_race()
        if race is None:
            return JSONResponse([])
        session_id = race.id

    readings = await storage.get_sensor_readings_for_session(session_id, mac)
    return JSONResponse(readings)
