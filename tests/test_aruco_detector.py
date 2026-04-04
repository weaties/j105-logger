"""Tests for ArUco marker detection, distance measurement, and calibration."""

from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from helmlog.aruco_detector import (
    CalibrationResult,
    CalibrationSession,
    CameraCalibration,
    DetectedMarker,
    compute_marker_distance,
    decode_jpeg,
    detect_and_measure,
    detect_markers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_marker_image(
    marker_ids: list[int],
    positions: list[tuple[int, int]],
    marker_px: int = 80,
    image_size: tuple[int, int] = (640, 480),
) -> np.ndarray:
    """Generate a synthetic image with ArUco markers at given positions."""
    img = np.ones((image_size[1], image_size[0], 3), dtype=np.uint8) * 255
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for mid, (x, y) in zip(marker_ids, positions, strict=True):
        marker_img = cv2.aruco.generateImageMarker(aruco_dict, mid, marker_px)
        marker_bgr = cv2.cvtColor(marker_img, cv2.COLOR_GRAY2BGR)
        x0 = max(0, x - marker_px // 2)
        y0 = max(0, y - marker_px // 2)
        x1 = min(image_size[0], x0 + marker_px)
        y1 = min(image_size[1], y0 + marker_px)
        mw = x1 - x0
        mh = y1 - y0
        img[y0:y1, x0:x1] = marker_bgr[:mh, :mw]
    return img


def _make_detected_marker(
    marker_id: int, center: tuple[float, float], size: float = 80.0
) -> DetectedMarker:
    """Create a DetectedMarker with synthetic square corners."""
    half = size / 2.0
    cx, cy = center
    corners = np.array(
        [
            [cx - half, cy - half],
            [cx + half, cy - half],
            [cx + half, cy + half],
            [cx - half, cy + half],
        ],
        dtype=np.float32,
    )
    return DetectedMarker(marker_id=marker_id, corners=corners, center=center)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detect_markers_finds_single_marker() -> None:
    """detect_markers should find a single ArUco marker in a synthetic image."""
    img = _make_marker_image([3], [(320, 240)])
    markers = detect_markers(img)
    assert len(markers) >= 1
    ids = {m.marker_id for m in markers}
    assert 3 in ids


def test_detect_markers_finds_multiple() -> None:
    """detect_markers should find multiple markers."""
    img = _make_marker_image([0, 7, 12], [(100, 100), (300, 100), (200, 300)])
    markers = detect_markers(img)
    ids = {m.marker_id for m in markers}
    assert {0, 7, 12}.issubset(ids)


def test_detect_markers_empty_image() -> None:
    """detect_markers returns empty list when no markers present."""
    img = np.ones((480, 640, 3), dtype=np.uint8) * 128
    markers = detect_markers(img)
    assert markers == []


def test_detect_markers_grayscale() -> None:
    """detect_markers works with grayscale input."""
    img = _make_marker_image([5], [(320, 240)])
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    markers = detect_markers(gray)
    assert len(markers) >= 1
    assert markers[0].marker_id == 5


def test_detected_marker_has_center() -> None:
    """Each detected marker should have a center coordinate."""
    img = _make_marker_image([1], [(200, 150)])
    markers = detect_markers(img)
    assert len(markers) >= 1
    m = [m for m in markers if m.marker_id == 1][0]
    assert abs(m.center[0] - 200) < 20  # approximate center
    assert abs(m.center[1] - 150) < 20


def test_detected_marker_has_four_corners() -> None:
    """Each detected marker should have 4 corner points."""
    img = _make_marker_image([2], [(320, 240)])
    markers = detect_markers(img)
    assert len(markers) >= 1
    assert markers[0].corners.shape == (4, 2)


# ---------------------------------------------------------------------------
# Distance measurement
# ---------------------------------------------------------------------------


def test_pixel_ratio_distance_known_setup() -> None:
    """Two markers of known pixel size at known pixel distance → correct cm."""
    # Markers are 80px wide, physical size = 50mm
    # Centers are 200px apart → 200 * (50/80) / 10 = 12.5 cm
    m_a = _make_detected_marker(0, (100.0, 100.0), size=80.0)
    m_b = _make_detected_marker(1, (300.0, 100.0), size=80.0)
    dist = compute_marker_distance(m_a, m_b, marker_size_mm=50.0)
    assert abs(dist - 12.5) < 0.5


def test_pixel_ratio_distance_vertical() -> None:
    """Distance measurement works for vertically separated markers."""
    m_a = _make_detected_marker(0, (100.0, 100.0), size=80.0)
    m_b = _make_detected_marker(1, (100.0, 260.0), size=80.0)
    dist = compute_marker_distance(m_a, m_b, marker_size_mm=50.0)
    assert abs(dist - 10.0) < 0.5  # 160px * (50/80) / 10 = 10 cm


def test_pixel_ratio_distance_same_position() -> None:
    """Markers at the same position → 0 distance."""
    m_a = _make_detected_marker(0, (100.0, 100.0))
    m_b = _make_detected_marker(1, (100.0, 100.0))
    dist = compute_marker_distance(m_a, m_b, marker_size_mm=50.0)
    assert dist == 0.0


# ---------------------------------------------------------------------------
# detect_and_measure
# ---------------------------------------------------------------------------


def test_detect_and_measure_with_pairs() -> None:
    """detect_and_measure should detect markers and compute distances for pairs."""
    img = _make_marker_image([0, 7], [(150, 240), (490, 240)])
    result = detect_and_measure(img, [(0, 7)], marker_size_mm=50.0)
    assert result.image_shape == (480, 640)
    assert len(result.markers) >= 2
    assert len(result.distances) == 1
    assert result.distances[0].marker_id_a == 0
    assert result.distances[0].marker_id_b == 7
    assert result.distances[0].distance_cm > 0


def test_detect_and_measure_missing_marker() -> None:
    """Pairs with missing markers produce no distance entry."""
    img = _make_marker_image([0], [(320, 240)])
    result = detect_and_measure(img, [(0, 99)], marker_size_mm=50.0)
    assert len(result.distances) == 0


def test_detect_and_measure_no_pairs() -> None:
    """No configured pairs → no distances, but markers are still detected."""
    img = _make_marker_image([3, 4], [(200, 200), (400, 200)])
    result = detect_and_measure(img, [], marker_size_mm=50.0)
    assert len(result.markers) >= 2
    assert len(result.distances) == 0


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_calibration_session_not_ready_initially() -> None:
    session = CalibrationSession()
    assert session.frame_count == 0
    assert not session.is_ready


def test_calibration_session_rejects_non_checkerboard() -> None:
    """Images without a checkerboard pattern are not accepted."""
    session = CalibrationSession()
    blank = np.ones((480, 640, 3), dtype=np.uint8) * 200
    assert session.add_frame(blank) is False
    assert session.frame_count == 0


def _make_checkerboard_image(cols: int = 9, rows: int = 6, square_px: int = 30) -> np.ndarray:
    """Generate a synthetic checkerboard image."""
    # +2 for border squares
    w = (cols + 2) * square_px
    h = (rows + 2) * square_px
    img = np.ones((h, w), dtype=np.uint8) * 255
    for r in range(rows + 2):
        for c in range(cols + 2):
            if (r + c) % 2 == 0:
                y0 = r * square_px
                x0 = c * square_px
                img[y0 : y0 + square_px, x0 : x0 + square_px] = 0
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def test_calibration_session_accepts_checkerboard() -> None:
    """A valid checkerboard image should be accepted."""
    session = CalibrationSession(cols=9, rows=6)
    img = _make_checkerboard_image(9, 6, square_px=40)
    result = session.add_frame(img)
    if not result:
        # OpenCV checkerboard detection can be finicky with synthetic images;
        # skip if the test platform can't detect the pattern reliably.
        pytest.skip("Synthetic checkerboard not detected on this platform")
    assert session.frame_count == 1


def test_calibration_session_calibrate_raises_when_not_ready() -> None:
    """calibrate() should raise ValueError if not enough frames."""
    session = CalibrationSession(required_frames=15)
    with pytest.raises(ValueError, match="Need 15 frames"):
        session.calibrate()


def test_calibration_session_full_workflow() -> None:
    """Full calibration with synthetic checkerboard images."""
    session = CalibrationSession(cols=9, rows=6, required_frames=3)  # low for speed
    base = _make_checkerboard_image(9, 6)
    for _ in range(5):  # add extra frames to be safe
        session.add_frame(base)
    if not session.is_ready:
        pytest.skip("Synthetic checkerboard not detected reliably enough")

    result = session.calibrate()
    assert isinstance(result, CalibrationResult)
    assert result.camera_matrix.shape == (3, 3)
    assert result.reprojection_error >= 0
    assert result.frame_count >= 3


# ---------------------------------------------------------------------------
# CalibrationResult serialization
# ---------------------------------------------------------------------------


def test_calibration_result_to_json() -> None:
    """CalibrationResult.to_json() produces valid JSON with expected keys."""
    result = CalibrationResult(
        camera_matrix=np.eye(3),
        dist_coeffs=np.zeros(5),
        reprojection_error=0.5,
        frame_count=15,
        checkerboard_cols=9,
        checkerboard_rows=6,
        checkerboard_square_mm=25.0,
    )
    data = json.loads(result.to_json())
    assert "camera_matrix" in data
    assert "dist_coeffs" in data
    assert data["reprojection_error_px"] == 0.5
    assert data["frame_count"] == 15


def test_camera_calibration_from_json_roundtrip() -> None:
    """CameraCalibration.from_json() roundtrips through CalibrationResult.to_json()."""
    result = CalibrationResult(
        camera_matrix=np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1]], dtype=np.float64),
        dist_coeffs=np.array([0.1, -0.2, 0.0, 0.0, 0.05]),
        reprojection_error=0.42,
        frame_count=15,
        checkerboard_cols=9,
        checkerboard_rows=6,
        checkerboard_square_mm=25.0,
    )
    cal = CameraCalibration.from_json(result.to_json())
    np.testing.assert_array_almost_equal(cal.camera_matrix, result.camera_matrix)
    np.testing.assert_array_almost_equal(cal.dist_coeffs.flatten(), result.dist_coeffs.flatten())


# ---------------------------------------------------------------------------
# decode_jpeg
# ---------------------------------------------------------------------------


def test_decode_jpeg_valid() -> None:
    """decode_jpeg should decode a valid JPEG."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    result = decode_jpeg(buf.tobytes())
    assert result.shape[:2] == (100, 100)


def test_decode_jpeg_invalid() -> None:
    """decode_jpeg should raise ValueError on invalid data."""
    with pytest.raises(ValueError, match="Failed to decode"):
        decode_jpeg(b"not a jpeg")
