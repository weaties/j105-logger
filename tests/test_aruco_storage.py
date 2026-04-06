"""Tests for ArUco storage methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Camera CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_aruco_cameras(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("shrouds", "192.168.1.10")
    assert cam_id > 0
    cameras = await storage.list_aruco_cameras()
    assert len(cameras) == 1
    assert cameras[0]["name"] == "shrouds"
    assert cameras[0]["ip"] == "192.168.1.10"
    assert cameras[0]["marker_size_mm"] == 50.0
    assert cameras[0]["calibration_state"] == "uncalibrated"


@pytest.mark.asyncio
async def test_get_aruco_camera(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("bow", "10.0.0.5")
    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["name"] == "bow"


@pytest.mark.asyncio
async def test_get_aruco_camera_by_name(storage: Storage) -> None:
    await storage.add_aruco_camera("stern", "10.0.0.6")
    cam = await storage.get_aruco_camera_by_name("stern")
    assert cam is not None
    assert cam["ip"] == "10.0.0.6"


@pytest.mark.asyncio
async def test_get_aruco_camera_not_found(storage: Storage) -> None:
    cam = await storage.get_aruco_camera(999)
    assert cam is None


@pytest.mark.asyncio
async def test_update_aruco_camera(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("test", "1.2.3.4")
    ok = await storage.update_aruco_camera(cam_id, ip="5.6.7.8", marker_size_mm=40.0)
    assert ok
    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["ip"] == "5.6.7.8"
    assert cam["marker_size_mm"] == 40.0


@pytest.mark.asyncio
async def test_update_aruco_camera_not_found(storage: Storage) -> None:
    ok = await storage.update_aruco_camera(999, ip="1.2.3.4")
    assert not ok


@pytest.mark.asyncio
async def test_delete_aruco_camera(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("del_me", "1.1.1.1")
    ok = await storage.delete_aruco_camera(cam_id)
    assert ok
    assert await storage.get_aruco_camera(cam_id) is None


@pytest.mark.asyncio
async def test_delete_aruco_camera_cascades(storage: Storage) -> None:
    """Deleting a camera should cascade to controls and measurements."""
    cam_id = await storage.add_aruco_camera("cascade", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("test_ctrl", cam_id, 0, 1)
    await storage.add_aruco_measurement(ctrl_id, 10.5)
    await storage.delete_aruco_camera(cam_id)
    controls = await storage.list_aruco_controls()
    assert all(c["camera_id"] != cam_id for c in controls)


@pytest.mark.asyncio
async def test_update_aruco_calibration(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("cal", "1.1.1.1")
    ok = await storage.update_aruco_calibration(cam_id, '{"test": true}', "calibrated")
    assert ok
    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["calibration_state"] == "calibrated"
    assert cam["calibration"] == '{"test": true}'


# ---------------------------------------------------------------------------
# Camera profiles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_profiles(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("profiled", "1.1.1.1")
    p_id = await storage.add_aruco_profile(cam_id, "daylight", '{"exposure_us": 5000}')
    assert p_id > 0
    profiles = await storage.list_aruco_profiles(cam_id)
    assert len(profiles) == 1
    assert profiles[0]["name"] == "daylight"


@pytest.mark.asyncio
async def test_activate_profile(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("multi_prof", "1.1.1.1")
    await storage.add_aruco_profile(cam_id, "day", "{}", is_active=True)
    p2 = await storage.add_aruco_profile(cam_id, "night", "{}")
    ok = await storage.activate_aruco_profile(p2)
    assert ok
    profiles = await storage.list_aruco_profiles(cam_id)
    active = {p["name"]: p["is_active"] for p in profiles}
    assert active["day"] == 0
    assert active["night"] == 1


@pytest.mark.asyncio
async def test_delete_profile(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("del_prof", "1.1.1.1")
    p_id = await storage.add_aruco_profile(cam_id, "temp", "{}")
    ok = await storage.delete_aruco_profile(p_id)
    assert ok
    profiles = await storage.list_aruco_profiles(cam_id)
    assert len(profiles) == 0


# ---------------------------------------------------------------------------
# Controls CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_controls(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("c1", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("Port upper shroud", cam_id, 7, 12)
    assert ctrl_id > 0
    controls = await storage.list_aruco_controls()
    assert len(controls) == 1
    assert controls[0]["name"] == "Port upper shroud"
    assert controls[0]["marker_id_a"] == 7
    assert controls[0]["marker_id_b"] == 12
    assert controls[0]["camera_name"] == "c1"


@pytest.mark.asyncio
async def test_get_aruco_control(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("c2", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("Backstay", cam_id, 0, 1)
    ctrl = await storage.get_aruco_control(ctrl_id)
    assert ctrl is not None
    assert ctrl["name"] == "Backstay"


@pytest.mark.asyncio
async def test_update_aruco_control(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("c3", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("Old name", cam_id, 0, 1)
    ok = await storage.update_aruco_control(ctrl_id, name="New name", tolerance_mm=2.5)
    assert ok
    ctrl = await storage.get_aruco_control(ctrl_id)
    assert ctrl is not None
    assert ctrl["name"] == "New name"
    assert ctrl["tolerance_mm"] == 2.5


@pytest.mark.asyncio
async def test_update_aruco_control_clear_tolerance(storage: Storage) -> None:
    """Setting tolerance_mm=None should clear it (use global default)."""
    cam_id = await storage.add_aruco_camera("c4", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tol_test", cam_id, 0, 1, tolerance_mm=3.0)
    ok = await storage.update_aruco_control(ctrl_id, tolerance_mm=None)
    assert ok
    ctrl = await storage.get_aruco_control(ctrl_id)
    assert ctrl is not None
    assert ctrl["tolerance_mm"] is None


@pytest.mark.asyncio
async def test_delete_aruco_control(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("c5", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("bye", cam_id, 0, 1)
    ok = await storage.delete_aruco_control(ctrl_id)
    assert ok
    assert await storage.get_aruco_control(ctrl_id) is None


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get_measurement(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("m1", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("m_ctrl", cam_id, 0, 1)
    m_id = await storage.add_aruco_measurement(ctrl_id, 15.3)
    assert m_id > 0
    latest = await storage.get_latest_aruco_measurement(ctrl_id)
    assert latest is not None
    assert latest["distance_cm"] == 15.3


@pytest.mark.asyncio
async def test_list_measurements_ordered(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("m2", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("m_ctrl2", cam_id, 0, 1)
    # Insert with explicit timestamps to guarantee ordering
    db = storage._conn()
    for i, dist in enumerate([10.0, 12.0, 14.0]):
        await db.execute(
            "INSERT INTO aruco_measurements (control_id, distance_cm, measured_at)"
            " VALUES (?, ?, ?)",
            (ctrl_id, dist, f"2026-04-04T12:00:0{i}Z"),
        )
    await db.commit()
    measurements = await storage.list_aruco_measurements(ctrl_id)
    assert len(measurements) == 3
    # Newest first
    assert measurements[0]["distance_cm"] == 14.0


@pytest.mark.asyncio
async def test_measurement_with_image_path(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("m3", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("m_ctrl3", cam_id, 0, 1)
    await storage.add_aruco_measurement(ctrl_id, 10.0, image_path="data/aruco/test.jpg")
    latest = await storage.get_latest_aruco_measurement(ctrl_id)
    assert latest is not None
    assert latest["image_path"] == "data/aruco/test.jpg"


@pytest.mark.asyncio
async def test_latest_measurement_none(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("m4", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("m_ctrl4", cam_id, 0, 1)
    latest = await storage.get_latest_aruco_measurement(ctrl_id)
    assert latest is None


# ---------------------------------------------------------------------------
# Trigger words
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_trigger_words(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("tw1", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tw_ctrl", cam_id, 0, 1)
    tw_id = await storage.add_aruco_trigger_word("backstay", ctrl_id)
    assert tw_id > 0
    words = await storage.list_aruco_trigger_words()
    assert len(words) == 1
    assert words[0]["phrase"] == "backstay"
    assert words[0]["control_name"] == "tw_ctrl"


@pytest.mark.asyncio
async def test_delete_trigger_word(storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("tw2", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tw_ctrl2", cam_id, 0, 1)
    tw_id = await storage.add_aruco_trigger_word("shrouds", ctrl_id)
    ok = await storage.delete_aruco_trigger_word(tw_id)
    assert ok
    words = await storage.list_aruco_trigger_words()
    assert len(words) == 0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_set_aruco_setting(storage: Storage) -> None:
    val = await storage.get_aruco_setting("tolerance_mm_default")
    assert val == "5.0"  # seeded by migration
    await storage.set_aruco_setting("tolerance_mm_default", "3.0")
    val = await storage.get_aruco_setting("tolerance_mm_default")
    assert val == "3.0"


@pytest.mark.asyncio
async def test_aruco_tolerance_per_control(storage: Storage) -> None:
    """Per-control tolerance overrides global default."""
    cam_id = await storage.add_aruco_camera("tol1", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tol_ctrl", cam_id, 0, 1, tolerance_mm=2.0)
    tol = await storage.get_aruco_tolerance_mm(ctrl_id)
    assert tol == 2.0


@pytest.mark.asyncio
async def test_aruco_tolerance_global_fallback(storage: Storage) -> None:
    """When no per-control tolerance, falls back to global default."""
    cam_id = await storage.add_aruco_camera("tol2", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tol_ctrl2", cam_id, 0, 1)
    tol = await storage.get_aruco_tolerance_mm(ctrl_id)
    assert tol == 5.0
