"""Tests for ArUco API routes (unified controls model)."""

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
# Unified Controls API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controls_list_has_seeded_params(client: httpx.AsyncClient) -> None:
    """The controls table should be seeded with canonical parameters on migration."""
    resp = await client.get("/api/controls")
    assert resp.status_code == 200
    data = resp.json()
    # Should have categories from the seeded parameters
    cat_names = [c["category"] for c in data["categories"]]
    assert "sail_controls" in cat_names
    # Vang should be present
    all_names = []
    for cat in data["categories"]:
        for ctrl in cat["controls"]:
            all_names.append(ctrl["name"])
    assert "vang" in all_names


@pytest.mark.asyncio
async def test_add_control_with_aruco(client: httpx.AsyncClient) -> None:
    """Create a control and attach ArUco marker config."""
    cam_resp = await client.post("/api/aruco/cameras", json={"name": "c1", "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]

    # Create control
    resp = await client.post(
        "/api/controls",
        json={"name": "port_shroud", "label": "Port shroud", "unit": "cm", "category": "rig"},
    )
    assert resp.status_code == 201
    ctrl_id = resp.json()["id"]

    # Attach ArUco config
    resp = await client.put(
        f"/api/controls/{ctrl_id}/aruco",
        json={"camera_id": cam_id, "marker_id_a": 7, "marker_id_b": 12},
    )
    assert resp.status_code == 200

    # Verify it shows up in aruco controls list
    resp = await client.get("/api/aruco/controls")
    assert resp.status_code == 200
    aruco = resp.json()
    names = [c["name"] for c in aruco]
    assert "port_shroud" in names


@pytest.mark.asyncio
async def test_delete_control(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/controls",
        json={"name": "del_ctrl", "label": "Del ctrl"},
    )
    ctrl_id = resp.json()["id"]
    resp = await client.delete(f"/api/controls/{ctrl_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_trigger_words_crud(client: httpx.AsyncClient) -> None:
    """Add and delete trigger words via unified endpoints."""
    resp = await client.post(
        "/api/controls",
        json={"name": "tw_test_ctrl", "label": "TW Test"},
    )
    ctrl_id = resp.json()["id"]

    # Add trigger word
    resp = await client.post(
        f"/api/controls/{ctrl_id}/trigger-words",
        json={"phrase": "backstay"},
    )
    assert resp.status_code == 201
    tw_id = resp.json()["id"]

    # Verify it's in the controls response
    resp = await client.get("/api/controls")
    data = resp.json()
    found = False
    for cat in data["categories"]:
        for ctrl in cat["controls"]:
            if ctrl["name"] == "tw_test_ctrl":
                assert any(tw["phrase"] == "backstay" for tw in ctrl["trigger_words"])
                found = True
    assert found

    # Delete
    resp = await client.delete(f"/api/controls/trigger-words/{tw_id}")
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


async def _setup_control_with_aruco(
    client: httpx.AsyncClient,
    name: str,
    camera_name: str,
    marker_a: int,
    marker_b: int,
) -> tuple[int, int]:
    """Helper: create camera + control + ArUco config. Returns (cam_id, ctrl_id)."""
    cam_resp = await client.post("/api/aruco/cameras", json={"name": camera_name, "ip": "1.1.1.1"})
    cam_id = cam_resp.json()["id"]
    ctrl_resp = await client.post(
        "/api/controls",
        json={"name": name, "label": name.replace("_", " ").title(), "unit": "cm"},
    )
    ctrl_id = ctrl_resp.json()["id"]
    await client.put(
        f"/api/controls/{ctrl_id}/aruco",
        json={"camera_id": cam_id, "marker_id_a": marker_a, "marker_id_b": marker_b},
    )
    return cam_id, ctrl_id


@pytest.mark.asyncio
async def test_ingest_image_detects_markers(client: httpx.AsyncClient) -> None:
    """Posting an image with markers should detect them and record to boat_settings."""
    await _setup_control_with_aruco(client, "test_ingest", "ingest_cam", 0, 7)

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
    await _setup_control_with_aruco(client, "tol_ingest", "tol_cam", 0, 7)

    jpeg = _make_marker_jpeg([0, 7], [(150, 240), (490, 240)])
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
# Preview API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_with_cached_thumbnail(client: httpx.AsyncClient, storage: Storage) -> None:
    """Preview endpoint should return cached thumbnail if available."""
    await storage.add_aruco_camera("prev_cam", "1.1.1.1")
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    storage._aruco_thumbnails = {"prev_cam": buf.tobytes()}  # type: ignore[attr-defined]

    resp = await client.get("/api/aruco/cameras/prev_cam/preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_preview_camera_not_found(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/aruco/cameras/nonexistent/preview")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_fallback_camera_unreachable(
    client: httpx.AsyncClient, storage: Storage
) -> None:
    """Preview should return 502 when camera has no cached thumbnail and is unreachable."""
    await storage.add_aruco_camera("offline_cam", "192.0.2.1")
    resp = await client.get("/api/aruco/cameras/offline_cam/preview")
    assert resp.status_code == 502


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


@pytest.mark.asyncio
async def test_calibration_reset(client: httpx.AsyncClient, storage: Storage) -> None:
    cam_id = await storage.add_aruco_camera("reset_cam", "1.1.1.1")
    await storage.update_aruco_calibration(cam_id, '{"test": true}', "calibrated")
    resp = await client.post(f"/api/aruco/cameras/{cam_id}/calibration/reset")
    assert resp.status_code == 200

    cam = await storage.get_aruco_camera(cam_id)
    assert cam is not None
    assert cam["calibration_state"] == "uncalibrated"


# ---------------------------------------------------------------------------
# Boat settings parameters (now DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boat_settings_parameters_from_db(client: httpx.AsyncClient) -> None:
    """The parameters endpoint should return data from the controls table."""
    resp = await client.get("/api/boat-settings/parameters")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert "weight_distribution_presets" in data
    # Vang should be present in sail_controls
    sail_cat = [c for c in data["categories"] if c["category"] == "sail_controls"]
    assert len(sail_cat) == 1
    names = [p["name"] for p in sail_cat[0]["parameters"]]
    assert "vang" in names
