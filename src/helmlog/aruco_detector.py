"""ArUco marker detection, distance measurement, and camera calibration.

Hardware-isolated module — no routes, no database calls. Works with
decoded images (numpy arrays) and returns structured results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedMarker:
    """A single ArUco marker detected in an image."""

    marker_id: int
    corners: np.ndarray  # shape (4, 2) — pixel coordinates of 4 corners
    center: tuple[float, float]  # center pixel coordinate


@dataclass(frozen=True)
class MarkerDistance:
    """Distance between a pair of markers."""

    marker_id_a: int
    marker_id_b: int
    distance_cm: float


@dataclass(frozen=True)
class DetectionResult:
    """Result of running ArUco detection on a single image."""

    markers: list[DetectedMarker]
    distances: list[MarkerDistance]
    image_shape: tuple[int, int]  # (height, width)


@dataclass(frozen=True)
class CalibrationResult:
    """Result of camera calibration from checkerboard images."""

    camera_matrix: np.ndarray  # 3x3 intrinsic matrix
    dist_coeffs: np.ndarray  # distortion coefficients
    reprojection_error: float  # RMS in pixels
    frame_count: int
    checkerboard_cols: int
    checkerboard_rows: int
    checkerboard_square_mm: float

    def to_json(self) -> str:
        """Serialize calibration to JSON for database storage."""
        return json.dumps(
            {
                "camera_matrix": self.camera_matrix.tolist(),
                "dist_coeffs": self.dist_coeffs.tolist(),
                "reprojection_error_px": round(self.reprojection_error, 4),
                "frame_count": self.frame_count,
                "checkerboard_cols": self.checkerboard_cols,
                "checkerboard_rows": self.checkerboard_rows,
                "checkerboard_square_mm": self.checkerboard_square_mm,
            }
        )


@dataclass
class CameraCalibration:
    """Parsed calibration data for a camera."""

    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray

    @classmethod
    def from_json(cls, data: str) -> CameraCalibration:
        """Parse calibration from stored JSON."""
        parsed = json.loads(data)
        return cls(
            camera_matrix=np.array(parsed["camera_matrix"], dtype=np.float64),
            dist_coeffs=np.array(parsed["dist_coeffs"], dtype=np.float64),
        )


# ---------------------------------------------------------------------------
# ArUco dictionary — 4x4_50 per issue decision
# ---------------------------------------------------------------------------

_ARUCO_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
_ARUCO_PARAMS = cv2.aruco.DetectorParameters()
_DETECTOR = cv2.aruco.ArucoDetector(_ARUCO_DICT, _ARUCO_PARAMS)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_markers(image: np.ndarray) -> list[DetectedMarker]:
    """Detect ArUco markers in an image.

    Args:
        image: BGR or grayscale image as numpy array.

    Returns:
        List of detected markers with their corners and centers.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image

    corners, ids, _ = _DETECTOR.detectMarkers(gray)

    if ids is None or len(corners) == 0:
        return []

    markers: list[DetectedMarker] = []
    for i, marker_id in enumerate(ids.flatten()):
        c = corners[i][0]  # shape (4, 2)
        cx = float(np.mean(c[:, 0]))
        cy = float(np.mean(c[:, 1]))
        markers.append(
            DetectedMarker(
                marker_id=int(marker_id),
                corners=c,
                center=(cx, cy),
            )
        )
    return markers


def compute_marker_distance(
    marker_a: DetectedMarker,
    marker_b: DetectedMarker,
    marker_size_mm: float,
    calibration: CameraCalibration | None = None,
) -> float:
    """Compute the distance in cm between two marker centers.

    If calibration data is available, uses camera intrinsics for accurate
    measurement. Otherwise falls back to a pixel-ratio estimate using
    the known marker size.

    Args:
        marker_a: First detected marker.
        marker_b: Second detected marker.
        marker_size_mm: Physical marker size in mm.
        calibration: Optional camera calibration for accurate measurement.

    Returns:
        Distance in centimeters.
    """
    if calibration is not None:
        return _calibrated_distance(marker_a, marker_b, marker_size_mm, calibration)
    return _pixel_ratio_distance(marker_a, marker_b, marker_size_mm)


def _pixel_ratio_distance(
    marker_a: DetectedMarker,
    marker_b: DetectedMarker,
    marker_size_mm: float,
) -> float:
    """Estimate distance using pixel-ratio from known marker size.

    Uses the average of both markers' pixel widths to estimate the
    mm/pixel ratio, then applies it to the center-to-center pixel distance.
    """
    size_a = _marker_pixel_size(marker_a)
    size_b = _marker_pixel_size(marker_b)
    avg_pixel_size = (size_a + size_b) / 2.0

    if avg_pixel_size < 1.0:
        return 0.0

    mm_per_pixel = marker_size_mm / avg_pixel_size

    dx = marker_a.center[0] - marker_b.center[0]
    dy = marker_a.center[1] - marker_b.center[1]
    pixel_dist = np.sqrt(dx * dx + dy * dy)

    return round(float(pixel_dist * mm_per_pixel / 10.0), 2)  # mm → cm


def _calibrated_distance(
    marker_a: DetectedMarker,
    marker_b: DetectedMarker,
    marker_size_mm: float,
    calibration: CameraCalibration,
) -> float:
    """Compute distance using solvePnP for each marker to get 3D positions."""
    half = marker_size_mm / 2.0
    obj_points = np.array(
        [
            [-half, half, 0],
            [half, half, 0],
            [half, -half, 0],
            [-half, -half, 0],
        ],
        dtype=np.float64,
    )

    positions: list[np.ndarray] = []
    for marker in (marker_a, marker_b):
        img_points = marker.corners.astype(np.float64)
        success, rvec, tvec = cv2.solvePnP(
            obj_points,
            img_points,
            calibration.camera_matrix,
            calibration.dist_coeffs,
        )
        if not success:
            return _pixel_ratio_distance(marker_a, marker_b, marker_size_mm)
        positions.append(tvec.flatten())

    dist_mm = float(np.linalg.norm(positions[0] - positions[1]))
    return round(dist_mm / 10.0, 2)  # mm → cm


def _marker_pixel_size(marker: DetectedMarker) -> float:
    """Return the average side length in pixels of a detected marker."""
    c = marker.corners  # (4, 2)
    sides = [np.linalg.norm(c[i] - c[(i + 1) % 4]) for i in range(4)]
    return float(np.mean(sides))


def detect_and_measure(
    image: np.ndarray,
    control_pairs: list[tuple[int, int]],
    marker_size_mm: float,
    calibration: CameraCalibration | None = None,
) -> DetectionResult:
    """Detect markers and compute distances for configured control pairs.

    Args:
        image: BGR or grayscale image.
        control_pairs: List of (marker_id_a, marker_id_b) pairs to measure.
        marker_size_mm: Physical marker size in mm.
        calibration: Optional camera calibration.

    Returns:
        DetectionResult with all detected markers and measured distances.
    """
    markers = detect_markers(image)
    h, w = image.shape[:2]

    marker_map = {m.marker_id: m for m in markers}
    distances: list[MarkerDistance] = []

    for id_a, id_b in control_pairs:
        if id_a in marker_map and id_b in marker_map:
            dist = compute_marker_distance(
                marker_map[id_a],
                marker_map[id_b],
                marker_size_mm,
                calibration,
            )
            distances.append(
                MarkerDistance(
                    marker_id_a=id_a,
                    marker_id_b=id_b,
                    distance_cm=dist,
                )
            )

    return DetectionResult(markers=markers, distances=distances, image_shape=(h, w))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass
class CalibrationSession:
    """Accumulates checkerboard frames for camera calibration."""

    cols: int = 9
    rows: int = 6
    square_mm: float = 25.0
    required_frames: int = 15
    obj_points: list[np.ndarray] = field(default_factory=list)
    img_points: list[np.ndarray] = field(default_factory=list)
    image_size: tuple[int, int] | None = None  # (width, height)

    @property
    def frame_count(self) -> int:
        return len(self.obj_points)

    @property
    def is_ready(self) -> bool:
        return self.frame_count >= self.required_frames

    def add_frame(self, image: np.ndarray) -> bool:
        """Try to find checkerboard corners in the image.

        Returns True if corners were found and added.
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image

        flags = (
            cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK
        )
        ret, corners = cv2.findChessboardCorners(gray, (self.cols, self.rows), flags)
        if not ret:
            return False

        # Subpixel refinement
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)

        # Build object points (0,0,0), (1,0,0), ... scaled by square_mm
        objp = np.zeros((self.rows * self.cols, 3), dtype=np.float64)
        objp[:, :2] = np.mgrid[0 : self.cols, 0 : self.rows].T.reshape(-1, 2) * self.square_mm

        self.obj_points.append(objp)
        self.img_points.append(corners.reshape(-1, 2))
        self.image_size = (gray.shape[1], gray.shape[0])
        return True

    def calibrate(self) -> CalibrationResult:
        """Run camera calibration. Raises ValueError if not enough frames."""
        if not self.is_ready:
            msg = f"Need {self.required_frames} frames, have {self.frame_count}"
            raise ValueError(msg)

        if self.image_size is None:
            msg = "No images processed"
            raise ValueError(msg)

        # Convert to the format cv2.calibrateCamera expects
        obj_pts = [pts.reshape(-1, 1, 3).astype(np.float32) for pts in self.obj_points]
        img_pts = [pts.reshape(-1, 1, 2).astype(np.float32) for pts in self.img_points]

        camera_matrix = np.zeros((3, 3), dtype=np.float64)
        dist_coeffs = np.zeros(5, dtype=np.float64)
        ret, mtx, dist, _rvecs, _tvecs = cv2.calibrateCamera(
            obj_pts,
            img_pts,
            self.image_size,
            camera_matrix,
            dist_coeffs,
        )

        return CalibrationResult(
            camera_matrix=mtx,
            dist_coeffs=dist,
            reprojection_error=ret,
            frame_count=self.frame_count,
            checkerboard_cols=self.cols,
            checkerboard_rows=self.rows,
            checkerboard_square_mm=self.square_mm,
        )


def decode_jpeg(data: bytes) -> np.ndarray:
    """Decode JPEG bytes to a BGR numpy array. Raises ValueError on failure."""
    arr = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        msg = "Failed to decode image"
        raise ValueError(msg)
    return image


def create_thumbnail(jpeg_data: bytes, max_width: int = 320) -> bytes:
    """Create a smaller JPEG thumbnail from full-size JPEG bytes.

    Returns the original bytes if decoding fails or image is already small.
    """
    arr = np.frombuffer(jpeg_data, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return jpeg_data
    h, w = image.shape[:2]
    if w <= max_width:
        return jpeg_data
    scale = max_width / w
    new_h = int(h * scale)
    thumb = cv2.resize(image, (max_width, new_h), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()
