"""ESP32 string potentiometer sensor support.

Manages device registration, calibration, zeroing, ADC-to-mm conversion,
and session-aware reading storage for ESP32-based control line sensors.

The ESP32 is a dumb sensor — it reads a cleaned raw ADC value and posts it.
HelmLog owns all intelligence: device state machine, calibration math,
session control, and configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


class DeviceState(StrEnum):
    """Lifecycle states for a sensor device."""

    UNREGISTERED = "unregistered"
    UNCALIBRATED = "uncalibrated"
    NEEDS_ZERO = "needs_zero"
    READY = "ready"


@dataclass(frozen=True)
class SensorDevice:
    """A registered ESP32 sensor device."""

    mac: str
    line_name: str | None
    friendly_name: str | None
    state: DeviceState
    # Calibration
    pulley_diameter_mm: float | None
    cal_adc_a: int | None
    cal_adc_b: int | None
    cal_distance_mm: float | None
    mm_per_adc_count: float | None
    total_travel_mm: float | None
    # Zeroing
    zero_offset_adc: int | None
    last_zeroed_at: str | None
    # Config
    sleep_idle_s: int
    sleep_active_s: int
    report_threshold_mm: float
    # Status
    last_seen_at: str | None
    last_battery_mv: int | None
    last_rssi: int | None
    created_at: str


@dataclass(frozen=True)
class SensorReading:
    """A single sensor reading to store."""

    session_id: int
    mac: str
    timestamp_utc: str
    raw_adc: int
    position_mm: float
    battery_mv: int | None


@dataclass(frozen=True)
class ReadingRequest:
    """Parsed POST body from an ESP32 sensor."""

    mac: str
    raw_adc: int
    battery_mv: int | None
    rssi: int | None
    button_pressed: bool
    estimated_utc: str | None


@dataclass(frozen=True)
class ReadingResponse:
    """Response sent back to the ESP32."""

    state: str
    sleep_seconds: int
    session_active: bool
    server_time_utc: str


def validate_mac(mac: str) -> bool:
    """Return True if *mac* matches ``XX:XX:XX:XX:XX:XX`` (uppercase hex)."""
    return bool(_MAC_RE.match(mac))


def compute_mm_per_adc(cal_adc_a: int, cal_adc_b: int, cal_distance_mm: float) -> float | None:
    """Compute the calibration factor from two-point calibration data.

    Returns ``None`` if the two ADC values are identical (division by zero).
    """
    diff = cal_adc_b - cal_adc_a
    if diff == 0:
        return None
    return cal_distance_mm / diff


def adc_to_mm(raw_adc: int, zero_offset_adc: int, mm_per_adc_count: float) -> float:
    """Convert a raw ADC reading to millimetres using calibration + zero offset."""
    return (raw_adc - zero_offset_adc) * mm_per_adc_count


def process_reading(
    device: SensorDevice,
    request: ReadingRequest,
    session_active: bool,
    current_session_id: int | None,
    last_stored_mm: float | None,
) -> tuple[ReadingResponse, SensorReading | None, dict[str, object] | None]:
    """Process an incoming sensor reading and determine the response.

    Returns a tuple of:
    - ReadingResponse to send back to the ESP32
    - SensorReading to store (or None if no reading should be stored)
    - dict of device field updates to apply (or None if no updates)
    """
    now_utc = datetime.now(UTC).isoformat()
    status_updates: dict[str, object] = {
        "last_seen_at": now_utc,
        "last_battery_mv": request.battery_mv,
        "last_rssi": request.rssi,
    }

    match device.state:
        case DeviceState.UNREGISTERED:
            return (
                ReadingResponse(
                    state="unregistered",
                    sleep_seconds=device.sleep_idle_s,
                    session_active=False,
                    server_time_utc=now_utc,
                ),
                None,
                status_updates,
            )

        case DeviceState.UNCALIBRATED:
            if request.button_pressed:
                # Two-point calibration: capture point A, then point B
                if device.cal_adc_a is None:
                    status_updates["cal_adc_a"] = request.raw_adc
                    return (
                        ReadingResponse(
                            state="calibrating_a",
                            sleep_seconds=2,
                            session_active=False,
                            server_time_utc=now_utc,
                        ),
                        None,
                        status_updates,
                    )
                # Point A already captured — capture point B
                status_updates["cal_adc_b"] = request.raw_adc
                return (
                    ReadingResponse(
                        state="calibrating_b",
                        sleep_seconds=2,
                        session_active=False,
                        server_time_utc=now_utc,
                    ),
                    None,
                    status_updates,
                )
            return (
                ReadingResponse(
                    state="uncalibrated",
                    sleep_seconds=device.sleep_idle_s,
                    session_active=False,
                    server_time_utc=now_utc,
                ),
                None,
                status_updates,
            )

        case DeviceState.NEEDS_ZERO:
            if request.button_pressed:
                status_updates["zero_offset_adc"] = request.raw_adc
                status_updates["last_zeroed_at"] = now_utc
                status_updates["state"] = DeviceState.READY
                return (
                    ReadingResponse(
                        state="zeroing",
                        sleep_seconds=2,
                        session_active=False,
                        server_time_utc=now_utc,
                    ),
                    None,
                    status_updates,
                )
            return (
                ReadingResponse(
                    state="needs_zero",
                    sleep_seconds=device.sleep_idle_s,
                    session_active=False,
                    server_time_utc=now_utc,
                ),
                None,
                status_updates,
            )

        case DeviceState.READY:
            if session_active and current_session_id is not None:
                status_updates["state"] = "active"
                reading = _maybe_store_reading(
                    device, request, current_session_id, now_utc, last_stored_mm
                )
                return (
                    ReadingResponse(
                        state="active",
                        sleep_seconds=device.sleep_active_s,
                        session_active=True,
                        server_time_utc=now_utc,
                    ),
                    reading,
                    status_updates,
                )
            return (
                ReadingResponse(
                    state="ready",
                    sleep_seconds=device.sleep_idle_s,
                    session_active=False,
                    server_time_utc=now_utc,
                ),
                None,
                status_updates,
            )

    # Should not be reached — all states handled above
    return (  # pragma: no cover
        ReadingResponse(
            state=device.state,
            sleep_seconds=device.sleep_idle_s,
            session_active=False,
            server_time_utc=now_utc,
        ),
        None,
        status_updates,
    )


def process_active_reading(
    device: SensorDevice,
    request: ReadingRequest,
    session_active: bool,
    current_session_id: int | None,
    last_stored_mm: float | None,
) -> tuple[ReadingResponse, SensorReading | None, dict[str, object] | None]:
    """Process a reading for a device whose persisted state is 'ready' but is logically active.

    This is called when the device was previously promoted to active (session was running)
    and is checking in again.
    """
    now_utc = datetime.now(UTC).isoformat()
    status_updates: dict[str, object] = {
        "last_seen_at": now_utc,
        "last_battery_mv": request.battery_mv,
        "last_rssi": request.rssi,
    }

    if not session_active:
        # Session ended — return to ready
        return (
            ReadingResponse(
                state="ready",
                sleep_seconds=device.sleep_idle_s,
                session_active=False,
                server_time_utc=now_utc,
            ),
            None,
            status_updates,
        )

    # Session still active — store reading if threshold met
    reading = (
        _maybe_store_reading(device, request, current_session_id, now_utc, last_stored_mm)
        if current_session_id is not None
        else None
    )
    return (
        ReadingResponse(
            state="active",
            sleep_seconds=device.sleep_active_s,
            session_active=True,
            server_time_utc=now_utc,
        ),
        reading,
        status_updates,
    )


def _maybe_store_reading(
    device: SensorDevice,
    request: ReadingRequest,
    session_id: int,
    now_utc: str,
    last_stored_mm: float | None,
) -> SensorReading | None:
    """Convert ADC → mm and decide whether to store a reading.

    Returns a SensorReading if the position changed enough, else None.
    """
    if device.mm_per_adc_count is None or device.zero_offset_adc is None:
        return None

    position_mm = adc_to_mm(request.raw_adc, device.zero_offset_adc, device.mm_per_adc_count)

    # First reading of session always stored
    if last_stored_mm is None:
        return SensorReading(
            session_id=session_id,
            mac=request.mac,
            timestamp_utc=now_utc,
            raw_adc=request.raw_adc,
            position_mm=round(position_mm, 2),
            battery_mv=request.battery_mv,
        )

    # Threshold-based noise suppression
    if abs(position_mm - last_stored_mm) < device.report_threshold_mm:
        return None

    return SensorReading(
        session_id=session_id,
        mac=request.mac,
        timestamp_utc=now_utc,
        raw_adc=request.raw_adc,
        position_mm=round(position_mm, 2),
        battery_mv=request.battery_mv,
    )
