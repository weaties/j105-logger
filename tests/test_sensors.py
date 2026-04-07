"""Tests for ESP32 string potentiometer sensor support (#432)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.sensors import (
    DeviceState,
    ReadingRequest,
    SensorDevice,
    adc_to_mm,
    compute_mm_per_adc,
    process_active_reading,
    process_reading,
    validate_mac,
)
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = "2026-04-06T18:30:00+00:00"


def _device(
    *,
    state: DeviceState = DeviceState.READY,
    mm_per_adc_count: float | None = 0.1,
    zero_offset_adc: int | None = 1000,
    sleep_idle_s: int = 30,
    sleep_active_s: int = 5,
    report_threshold_mm: float = 2.0,
    total_travel_mm: float | None = 500.0,
) -> SensorDevice:
    return SensorDevice(
        mac="AA:BB:CC:DD:EE:FF",
        line_name="backstay",
        friendly_name=None,
        state=state,
        pulley_diameter_mm=None,
        cal_adc_a=1000,
        cal_adc_b=2000,
        cal_distance_mm=100.0,
        mm_per_adc_count=mm_per_adc_count,
        total_travel_mm=total_travel_mm,
        zero_offset_adc=zero_offset_adc,
        last_zeroed_at="2026-04-06T10:00:00+00:00",
        sleep_idle_s=sleep_idle_s,
        sleep_active_s=sleep_active_s,
        report_threshold_mm=report_threshold_mm,
        last_seen_at=None,
        last_battery_mv=None,
        last_rssi=None,
        last_raw_adc=None,
        created_at="2026-04-06T09:00:00+00:00",
    )


def _request(
    *,
    raw_adc: int = 1500,
    button_pressed: bool = False,
    battery_mv: int = 3850,
    rssi: int = -62,
) -> ReadingRequest:
    return ReadingRequest(
        mac="AA:BB:CC:DD:EE:FF",
        raw_adc=raw_adc,
        battery_mv=battery_mv,
        rssi=rssi,
        button_pressed=button_pressed,
        estimated_utc="2026-04-06T18:30:00Z",
    )


# ---------------------------------------------------------------------------
# Unit tests — validate_mac
# ---------------------------------------------------------------------------


class TestValidateMac:
    def test_valid_mac(self) -> None:
        assert validate_mac("AA:BB:CC:DD:EE:FF") is True

    def test_lowercase_rejected(self) -> None:
        assert validate_mac("aa:bb:cc:dd:ee:ff") is False

    def test_missing_colons(self) -> None:
        assert validate_mac("AABBCCDDEEFF") is False

    def test_too_short(self) -> None:
        assert validate_mac("AA:BB:CC") is False

    def test_empty(self) -> None:
        assert validate_mac("") is False


# ---------------------------------------------------------------------------
# Unit tests — compute_mm_per_adc
# ---------------------------------------------------------------------------


class TestComputeMmPerAdc:
    def test_normal_calibration(self) -> None:
        result = compute_mm_per_adc(1000, 2000, 100.0)
        assert result == pytest.approx(0.1)

    def test_identical_adc_returns_none(self) -> None:
        assert compute_mm_per_adc(1000, 1000, 100.0) is None

    def test_negative_direction(self) -> None:
        result = compute_mm_per_adc(2000, 1000, 100.0)
        assert result == pytest.approx(-0.1)


# ---------------------------------------------------------------------------
# Unit tests — adc_to_mm
# ---------------------------------------------------------------------------


class TestAdcToMm:
    def test_zero_offset(self) -> None:
        assert adc_to_mm(1500, 1000, 0.1) == pytest.approx(50.0)

    def test_at_zero(self) -> None:
        assert adc_to_mm(1000, 1000, 0.1) == pytest.approx(0.0)

    def test_negative_position(self) -> None:
        assert adc_to_mm(500, 1000, 0.1) == pytest.approx(-50.0)


# ---------------------------------------------------------------------------
# Unit tests — process_reading state machine
# ---------------------------------------------------------------------------


class TestProcessReadingUnregistered:
    def test_unregistered_returns_unregistered(self) -> None:
        dev = _device(state=DeviceState.UNREGISTERED)
        resp, reading, updates = process_reading(dev, _request(), False, None, None)
        assert resp.state == "unregistered"
        assert resp.session_active is False
        assert resp.sleep_seconds == 30
        assert reading is None

    def test_unregistered_with_session_still_unregistered(self) -> None:
        dev = _device(state=DeviceState.UNREGISTERED)
        resp, reading, _ = process_reading(dev, _request(), True, 1, None)
        assert resp.state == "unregistered"
        assert resp.session_active is False
        assert reading is None


class TestProcessReadingUncalibrated:
    def test_uncalibrated_no_button(self) -> None:
        dev = _device(state=DeviceState.UNCALIBRATED)
        resp, reading, _ = process_reading(dev, _request(), False, None, None)
        assert resp.state == "uncalibrated"
        assert reading is None

    def test_uncalibrated_button_captures_point_a(self) -> None:
        dev = _device(state=DeviceState.UNCALIBRATED)
        # No cal_adc_a yet — should capture point A
        dev_no_cal = SensorDevice(
            mac=dev.mac,
            line_name=dev.line_name,
            friendly_name=dev.friendly_name,
            state=DeviceState.UNCALIBRATED,
            pulley_diameter_mm=None,
            cal_adc_a=None,
            cal_adc_b=None,
            cal_distance_mm=None,
            mm_per_adc_count=None,
            total_travel_mm=None,
            zero_offset_adc=None,
            last_zeroed_at=None,
            sleep_idle_s=30,
            sleep_active_s=5,
            report_threshold_mm=2.0,
            last_seen_at=None,
            last_battery_mv=None,
            last_rssi=None,
            last_raw_adc=None,
            created_at="2026-04-06T09:00:00+00:00",
        )
        resp, reading, updates = process_reading(
            dev_no_cal, _request(raw_adc=1000, button_pressed=True), False, None, None
        )
        assert resp.state == "calibrating_a"
        assert resp.sleep_seconds == 2
        assert updates is not None
        assert updates["cal_adc_a"] == 1000
        assert reading is None

    def test_uncalibrated_button_captures_point_b(self) -> None:
        # cal_adc_a already set — should capture point B
        dev_with_a = SensorDevice(
            mac="AA:BB:CC:DD:EE:FF",
            line_name="backstay",
            friendly_name=None,
            state=DeviceState.UNCALIBRATED,
            pulley_diameter_mm=None,
            cal_adc_a=1000,
            cal_adc_b=None,
            cal_distance_mm=None,
            mm_per_adc_count=None,
            total_travel_mm=None,
            zero_offset_adc=None,
            last_zeroed_at=None,
            sleep_idle_s=30,
            sleep_active_s=5,
            report_threshold_mm=2.0,
            last_seen_at=None,
            last_battery_mv=None,
            last_rssi=None,
            last_raw_adc=None,
            created_at="2026-04-06T09:00:00+00:00",
        )
        resp, reading, updates = process_reading(
            dev_with_a, _request(raw_adc=2000, button_pressed=True), False, None, None
        )
        assert resp.state == "calibrating_b"
        assert updates is not None
        assert updates["cal_adc_b"] == 2000
        assert reading is None


class TestProcessReadingNeedsZero:
    def test_needs_zero_no_button(self) -> None:
        dev = _device(state=DeviceState.NEEDS_ZERO, zero_offset_adc=None)
        resp, reading, updates = process_reading(dev, _request(), False, None, None)
        assert resp.state == "needs_zero"
        assert reading is None

    def test_needs_zero_button_transitions_to_ready(self) -> None:
        dev = _device(state=DeviceState.NEEDS_ZERO, zero_offset_adc=None)
        resp, reading, updates = process_reading(
            dev, _request(raw_adc=2000, button_pressed=True), False, None, None
        )
        assert resp.state == "zeroing"
        assert resp.sleep_seconds == 2
        assert updates is not None
        assert updates["zero_offset_adc"] == 2000
        assert updates["state"] == DeviceState.READY
        assert reading is None


class TestProcessReadingReady:
    def test_ready_no_session(self) -> None:
        dev = _device()
        resp, reading, _ = process_reading(dev, _request(), False, None, None)
        assert resp.state == "ready"
        assert resp.sleep_seconds == 30
        assert resp.session_active is False
        assert reading is None

    def test_ready_with_session_transitions_to_active(self) -> None:
        dev = _device()
        resp, reading, updates = process_reading(dev, _request(), True, 1, None)
        assert resp.state == "active"
        assert resp.sleep_seconds == 5
        assert resp.session_active is True
        # First reading of session should be stored
        assert reading is not None
        assert reading.session_id == 1
        assert reading.position_mm == pytest.approx(50.0)


class TestProcessActiveReading:
    def test_active_session_still_running(self) -> None:
        dev = _device()
        resp, reading, _ = process_active_reading(dev, _request(), True, 1, 45.0)
        assert resp.state == "active"
        assert resp.session_active is True
        # position_mm = (1500 - 1000) * 0.1 = 50.0, delta from 45.0 = 5.0 >= 2.0
        assert reading is not None
        assert reading.position_mm == pytest.approx(50.0)

    def test_active_below_threshold_suppressed(self) -> None:
        dev = _device()
        # last_stored = 49.5, new = 50.0, delta = 0.5 < 2.0
        resp, reading, _ = process_active_reading(dev, _request(), True, 1, 49.5)
        assert resp.state == "active"
        assert reading is None

    def test_active_session_ends(self) -> None:
        dev = _device()
        resp, reading, _ = process_active_reading(dev, _request(), False, None, 50.0)
        assert resp.state == "ready"
        assert resp.session_active is False
        assert reading is None

    def test_active_first_reading_always_stored(self) -> None:
        dev = _device()
        resp, reading, _ = process_reading(dev, _request(), True, 1, None)
        assert reading is not None

    def test_no_calibration_no_reading(self) -> None:
        dev = _device(mm_per_adc_count=None, zero_offset_adc=None)
        resp, reading, _ = process_reading(dev, _request(), True, 1, None)
        assert resp.state == "active"
        assert reading is None


# ---------------------------------------------------------------------------
# Integration tests — API endpoint via ASGI transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_reading_unknown_mac_registers(storage: Storage) -> None:
    """POST /api/sensor/reading with unknown MAC creates a new device."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sensor/reading",
            json={
                "mac": "AA:BB:CC:DD:EE:01",
                "raw_adc": 2000,
                "battery_mv": 3800,
                "rssi": -55,
                "button_pressed": False,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "unregistered"
    assert data["session_active"] is False
    assert "server_time_utc" in data

    # Verify device was persisted
    device = await storage.get_sensor_device("AA:BB:CC:DD:EE:01")
    assert device is not None
    assert device.state == DeviceState.UNREGISTERED


@pytest.mark.asyncio
async def test_sensor_reading_invalid_mac_returns_422(storage: Storage) -> None:
    """POST /api/sensor/reading with invalid MAC returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sensor/reading",
            json={"mac": "invalid", "raw_adc": 2000},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sensor_reading_missing_adc_returns_422(storage: Storage) -> None:
    """POST /api/sensor/reading without raw_adc returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sensor/reading",
            json={"mac": "AA:BB:CC:DD:EE:01"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sensor_reading_during_session_stores_reading(storage: Storage) -> None:
    """Active session + calibrated device should store a sensor reading."""
    app = create_app(storage)
    mac = "AA:BB:CC:DD:EE:02"

    # Register + calibrate + zero the device
    await storage.register_sensor_device(mac)
    await storage.update_sensor_device(
        mac,
        {
            "state": "ready",
            "line_name": "backstay",
            "cal_adc_a": 1000,
            "cal_adc_b": 2000,
            "cal_distance_mm": 100.0,
            "mm_per_adc_count": 0.1,
            "zero_offset_adc": 1000,
            "last_zeroed_at": "2026-04-06T10:00:00+00:00",
        },
    )

    # Start a session
    await storage.start_race(
        event="TestRegatta",
        start_utc=datetime(2026, 4, 6, 18, 0, 0, tzinfo=UTC),
        date_str="2026-04-06",
        race_num=1,
        name="2026-04-06 TestRegatta 1",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sensor/reading",
            json={
                "mac": mac,
                "raw_adc": 1500,
                "battery_mv": 3850,
                "rssi": -62,
                "button_pressed": False,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "active"
    assert data["session_active"] is True

    # Verify reading was stored
    readings = await storage.get_sensor_readings_for_session(1, mac)
    assert len(readings) == 1
    assert readings[0]["position_mm"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_sensor_reading_threshold_suppression(storage: Storage) -> None:
    """Readings below report_threshold_mm should not be stored."""
    app = create_app(storage)
    mac = "AA:BB:CC:DD:EE:03"

    await storage.register_sensor_device(mac)
    await storage.update_sensor_device(
        mac,
        {
            "state": "ready",
            "line_name": "cunningham",
            "cal_adc_a": 0,
            "cal_adc_b": 4095,
            "cal_distance_mm": 409.5,
            "mm_per_adc_count": 0.1,
            "zero_offset_adc": 0,
            "report_threshold_mm": 5.0,
        },
    )

    await storage.start_race(
        event="TestRegatta",
        start_utc=datetime(2026, 4, 6, 18, 0, 0, tzinfo=UTC),
        date_str="2026-04-06",
        race_num=1,
        name="2026-04-06 TestRegatta 1",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # First reading — always stored
        resp1 = await client.post(
            "/api/sensor/reading",
            json={"mac": mac, "raw_adc": 100, "battery_mv": 3800, "rssi": -60},
        )
        assert resp1.status_code == 200

        # Second reading — within threshold (delta = 1mm < 5mm), should NOT store
        resp2 = await client.post(
            "/api/sensor/reading",
            json={"mac": mac, "raw_adc": 110, "battery_mv": 3800, "rssi": -60},
        )
        assert resp2.status_code == 200

        # Third reading — exceeds threshold (delta = 100mm > 5mm), should store
        resp3 = await client.post(
            "/api/sensor/reading",
            json={"mac": mac, "raw_adc": 1100, "battery_mv": 3800, "rssi": -60},
        )
        assert resp3.status_code == 200

    readings = await storage.get_sensor_readings_for_session(1, mac)
    assert len(readings) == 2  # first + third


@pytest.mark.asyncio
async def test_sensor_zeroing_via_button(storage: Storage) -> None:
    """Button press in needs_zero state sets zero offset and transitions to ready."""
    app = create_app(storage)
    mac = "AA:BB:CC:DD:EE:04"

    await storage.register_sensor_device(mac)
    await storage.update_sensor_device(
        mac,
        {
            "state": "needs_zero",
            "line_name": "outhaul",
            "cal_adc_a": 0,
            "cal_adc_b": 4095,
            "cal_distance_mm": 409.5,
            "mm_per_adc_count": 0.1,
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sensor/reading",
            json={
                "mac": mac,
                "raw_adc": 2048,
                "battery_mv": 3900,
                "rssi": -50,
                "button_pressed": True,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "zeroing"

    # Verify device transitioned
    device = await storage.get_sensor_device(mac)
    assert device is not None
    assert device.state == DeviceState.READY
    assert device.zero_offset_adc == 2048


@pytest.mark.asyncio
async def test_sensor_admin_update_transitions_state(storage: Storage) -> None:
    """Assigning line_name transitions unregistered → uncalibrated."""
    mac = "AA:BB:CC:DD:EE:05"
    await storage.register_sensor_device(mac)

    await storage.update_sensor_device(mac, {"line_name": "vang", "state": "uncalibrated"})

    device = await storage.get_sensor_device(mac)
    assert device is not None
    assert device.state == DeviceState.UNCALIBRATED
    assert device.line_name == "vang"


# ---------------------------------------------------------------------------
# Storage method tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_sensor_device_lifecycle(storage: Storage) -> None:
    """Register, update, list, and delete a sensor device."""
    mac = "AA:BB:CC:DD:EE:10"

    # Register
    await storage.register_sensor_device(mac)
    device = await storage.get_sensor_device(mac)
    assert device is not None
    assert device.state == DeviceState.UNREGISTERED

    # Update
    await storage.update_sensor_device(mac, {"line_name": "backstay", "state": "uncalibrated"})
    device = await storage.get_sensor_device(mac)
    assert device is not None
    assert device.line_name == "backstay"
    assert device.state == DeviceState.UNCALIBRATED

    # List
    devices = await storage.list_sensor_devices()
    assert len(devices) == 1
    assert devices[0].mac == mac

    # Delete
    await storage.delete_sensor_device(mac)
    device = await storage.get_sensor_device(mac)
    assert device is None


@pytest.mark.asyncio
async def test_storage_sensor_readings(storage: Storage) -> None:
    """Store and retrieve sensor readings."""
    mac = "AA:BB:CC:DD:EE:11"

    await storage.register_sensor_device(mac)

    # Start a race for session_id
    race = await storage.start_race(
        event="Test",
        start_utc=datetime(2026, 4, 6, 18, 0, 0, tzinfo=UTC),
        date_str="2026-04-06",
        race_num=1,
        name="2026-04-06 Test 1",
    )

    # Store readings
    await storage.store_sensor_reading(
        session_id=race.id,
        mac=mac,
        timestamp_utc="2026-04-06T18:00:01+00:00",
        raw_adc=1500,
        position_mm=50.0,
        battery_mv=3850,
    )
    await storage.store_sensor_reading(
        session_id=race.id,
        mac=mac,
        timestamp_utc="2026-04-06T18:00:06+00:00",
        raw_adc=1600,
        position_mm=60.0,
        battery_mv=3840,
    )

    # Get last reading
    last_mm = await storage.get_last_sensor_reading_mm(mac, race.id)
    assert last_mm == pytest.approx(60.0)

    # Get all readings
    readings = await storage.get_sensor_readings_for_session(race.id, mac)
    assert len(readings) == 2

    # Get all readings without mac filter
    all_readings = await storage.get_sensor_readings_for_session(race.id)
    assert len(all_readings) == 2
