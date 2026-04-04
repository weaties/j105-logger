"""Tests for ArUco API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import httpx
import numpy as np
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:  # type: ignore[misc]
    """Authenticated admin client (auth disabled via env)."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Camera API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cameras_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/aruco/cameras")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_add_camera(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/aruco/cameras",
        json={
            "name": "shrouds",
            "ip": "192.168.1.10",
            "marker_size_mm": 40.0,
            "capture_interval_s": 30,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data

    resp = await client.get("/api/aruco/cameras")
    cameras = resp.json()
    assert len(cameras) == 1
    assert cameras[0]["name"] == "shrouds"
    assert cameras[0]["marker_size_mm"] == 40.0


@pytest.mark.asyncio
async def test_add_camera_missing_fields(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/aruco/cameras", json={"name": "x"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_camera(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/aruco/cameras", json={"name": "upd", "ip": "1.1.1.1"})
    cam_id = resp.json()["id"]
    resp = await client.put(f"/api/aruco/cameras/{cam_id}", json={"ip": "2.2.2.2"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_camera(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/aruco/cameras", json={"name": "del", "ip": "1.1.1.1"})
    cam_id = resp.json()["id"]
    resp = await client.delete(f"/api/aruco/cameras/{cam_id}")
    assert resp.status_code == 200
    resp = await client.get("/api/aruco/cameras")
    assert resp.json() == []


@pytest.mark.asyncio
async def test_delete_camera_not_found(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/aruco/cameras/999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Controls API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_controls(client: httpx.AsyncClient) -> None:
    cam_resp = await client.post("/api/aruco/cameras", json={"name": "c1", "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]

    resp = await client.post(
        "/api/aruco/controls",
        json={
            "name": "Port shroud",
            "camera_id": cam_id,
            "marker_id_a": 7,
            "marker_id_b": 12,
        },
    )
    assert resp.status_code == 201

    resp = await client.get("/api/aruco/controls")
    controls = resp.json()
    assert len(controls) == 1
    assert controls[0]["name"] == "Port shroud"


@pytest.mark.asyncio
async def test_add_control_missing_fields(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/aruco/controls", json={"name": "incomplete"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_control(client: httpx.AsyncClient) -> None:
    cam_resp = await client.post("/api/aruco/cameras", json={"name": "c2", "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]
    ctrl_resp = await client.post(
        "/api/aruco/controls",
        json={
            "name": "Del ctrl",
            "camera_id": cam_id,
            "marker_id_a": 0,
            "marker_id_b": 1,
        },
    )
    ctrl_id = ctrl_resp.json()["id"]
    resp = await client.delete(f"/api/aruco/controls/{ctrl_id}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Image ingestion
# ---------------------------------------------------------------------------


def _make_marker_jpeg(marker_ids: list[int], positions: list[tuple[int, int]]) -> bytes:
    """Generate a JPEG with ArUco markers."""
    img = np.ones((480, 640, 3), dtype=np.uint8) * 255
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for mid, (x, y) in zip(marker_ids, positions, strict=True):
        marker_img = cv2.aruco.generateImageMarker(aruco_dict, mid, 80)
        marker_bgr = cv2.cvtColor(marker_img, cv2.COLOR_GRAY2BGR)
        x0, y0 = x - 40, y - 40
        img[y0 : y0 + 80, x0 : x0 + 80] = marker_bgr
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


@pytest.mark.asyncio
async def test_ingest_image_detects_markers(client: httpx.AsyncClient) -> None:
    """Posting an image with markers should detect them and record measurements."""
    cam_resp = await client.post("/api/aruco/cameras", json={"name": "ingest_cam", "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]
    await client.post(
        "/api/aruco/controls",
        json={
            "name": "Test control",
            "camera_id": cam_id,
            "marker_id_a": 0,
            "marker_id_b": 7,
        },
    )

    jpeg = _make_marker_jpeg([0, 7], [(150, 240), (490, 240)])
    resp = await client.post(
        "/api/aruco/cameras/ingest_cam/image",
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["markers_detected"] >= 2
    assert data["measurements_recorded"] >= 1


@pytest.mark.asyncio
async def test_ingest_image_camera_not_found(client: httpx.AsyncClient) -> None:
    jpeg = _make_marker_jpeg([0], [(320, 240)])
    resp = await client.post(
        "/api/aruco/cameras/nonexistent/image",
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingest_image_within_tolerance(client: httpx.AsyncClient) -> None:
    """A second image with similar distance should not record a new measurement."""
    cam_resp = await client.post("/api/aruco/cameras", json={"name": "tol_cam", "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]
    await client.post(
        "/api/aruco/controls",
        json={
            "name": "Tol control",
            "camera_id": cam_id,
            "marker_id_a": 0,
            "marker_id_b": 7,
        },
    )

    jpeg = _make_marker_jpeg([0, 7], [(150, 240), (490, 240)])
    # First image — records measurement
    resp1 = await client.post(
        "/api/aruco/cameras/tol_cam/image",
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
    )
    assert resp1.json()["measurements_recorded"] >= 1

    # Same image — should not record (within tolerance)
    resp2 = await client.post(
        "/api/aruco/cameras/tol_cam/image",
        files={"image": ("frame.jpg", jpeg, "image/jpeg")},
    )
    assert resp2.json()["measurements_recorded"] == 0


# ---------------------------------------------------------------------------
# Measurements API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_measurements(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("latest_cam", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("latest_ctrl", cam_id, 0, 1)
    await storage.add_aruco_measurement(ctrl_id, 10.5)

    resp = await client.get("/api/aruco/controls/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    found = [d for d in data if d["control_name"] == "latest_ctrl"]
    assert len(found) == 1
    assert found[0]["latest"]["distance_cm"] == 10.5


@pytest.mark.asyncio
async def test_measurement_history(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("hist_cam", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("hist_ctrl", cam_id, 0, 1)
    await storage.add_aruco_measurement(ctrl_id, 10.0)
    await storage.add_aruco_measurement(ctrl_id, 12.0)

    resp = await client.get(f"/api/aruco/controls/{ctrl_id}/measurements")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


# ---------------------------------------------------------------------------
# Trigger words API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_words_crud(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("tw_cam", "1.1.1.1")
    ctrl_id = await storage.add_aruco_control("tw_ctrl", cam_id, 0, 1)

    # Add
    resp = await client.post(
        "/api/aruco/trigger-words",
        json={
            "phrase": "backstay",
            "control_id": ctrl_id,
        },
    )
    assert resp.status_code == 201
    tw_id = resp.json()["id"]

    # List
    resp = await client.get("/api/aruco/trigger-words")
    assert resp.status_code == 200
    words = resp.json()
    assert len(words) == 1
    assert words[0]["phrase"] == "backstay"

    # Delete
    resp = await client.delete(f"/api/aruco/trigger-words/{tw_id}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_get_and_update(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/aruco/settings")
    assert resp.status_code == 200
    assert resp.json()["tolerance_mm_default"] == 5.0

    resp = await client.put("/api/aruco/settings", json={"tolerance_mm_default": 3.0})
    assert resp.status_code == 200

    resp = await client.get("/api/aruco/settings")
    assert resp.json()["tolerance_mm_default"] == 3.0


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_aruco_page(client: httpx.AsyncClient) -> None:
    resp = await client.get("/admin/aruco")
    assert resp.status_code == 200
    assert "ArUco" in resp.text


# ---------------------------------------------------------------------------
# Calibration API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibration_start(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("cal_cam", "1.1.1.1")
    resp = await client.post(f"/api/aruco/cameras/{cam_id}/calibration/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "capturing"

    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["calibration_state"] == "capturing"


@pytest.mark.asyncio
async def test_calibration_reset(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("reset_cam", "1.1.1.1")
    await storage.update_aruco_calibration(cam_id, '{"test": true}', "calibrated")
    resp = await client.post(f"/api/aruco/cameras/{cam_id}/calibration/reset")
    assert resp.status_code == 200

    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["calibration_state"] == "uncalibrated"
