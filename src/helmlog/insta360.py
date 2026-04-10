"""Insta360 X4 file discovery and recording grouping.

Scans an SD card mount point for video files, parses their
timestamp-based filenames, and groups them into logical recordings.

The Insta360 X4 saves in two formats depending on recording mode:
  - **360° mode** → ``.insv`` (dual-fisheye, needs stitching)
  - **Single-lens / wide-angle** → ``.mp4`` (ready to upload)

File naming convention (Insta360 X4):
    VID_YYYYMMDD_HHMMSS_XX_NNN.insv   (360° mode)
    VID_YYYYMMDD_HHMMSS_XX_NNN.mp4    (single-lens mode)
      XX  = lens: 00 (back / main), 10 (front), 01/11 (LRV preview)
      NNN = segment number (000, 001, ...)

Only ``_00_`` files are included in recording segments; for .insv the
stitcher automatically pairs front+back.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Matches: VID_YYYYMMDD_HHMMSS_XX_NNN.insv or .mp4  (also PRO_VID_ prefix)
_VID_RE = re.compile(r"^(?:PRO_)?VID_(\d{8}_\d{6})_(\d{2})_(\d{3})\.(insv|mp4)$")


@dataclass(frozen=True)
class InsvFileInfo:
    """Parsed metadata from an Insta360 video filename."""

    timestamp_str: str  # YYYYMMDD_HHMMSS
    lens: str  # 00 = back/main, 10 = front
    segment: int  # 0-based segment number
    extension: str  # "insv" or "mp4"


def parse_insv_filename(name: str) -> InsvFileInfo | None:
    """Parse an Insta360 video filename into structured metadata.

    Supports both ``.insv`` (360° mode) and ``.mp4`` (single-lens mode).
    Returns None for non-VID files (LRV previews, photos, etc.).
    """
    m = _VID_RE.match(name)
    if m is None:
        return None
    return InsvFileInfo(
        timestamp_str=m.group(1),
        lens=m.group(2),
        segment=int(m.group(3)),
        extension=m.group(4),
    )


# ---------------------------------------------------------------------------
# Recording dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstaRecording:
    """A single continuous recording (may span multiple segments).

    Attributes:
        timestamp_str: Recording start time from filename (YYYYMMDD_HHMMSS).
        segments: Ordered list of video file paths.
        total_size_bytes: Combined size of all segment files.
        needs_stitching: True for .insv (360°), False for .mp4 (single-lens).
    """

    timestamp_str: str
    segments: list[Path] = field(default_factory=list)
    total_size_bytes: int = 0
    needs_stitching: bool = True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def probe_duration_s(path: Path) -> float | None:
    """Return the video duration in seconds, or ``None`` on probe failure.

    Used by the import step to compute an accurate ``end_utc`` for session
    matching — far more reliable than the old ``start_utc + 2 hours``
    heuristic, which would over-match recordings to sessions hours away.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.debug("ffprobe duration failed for {}: {}", path, exc)
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_dual_fisheye(path: Path) -> bool:
    """Return True if a video file is X4 dual-fisheye 360°.

    Probes the container with ``ffprobe`` and considers a file to be
    dual-fisheye when it contains **two** video streams of identical,
    square dimensions (the X4 5.7K mode is 2880×2880 × 2; 8K mode is
    3840×3840 × 2). Falls back to ``False`` on any probe failure so the
    pipeline degrades to a direct upload rather than blowing up.

    The X4 writes 360° captures as either ``.insv`` or ``.mp4``
    depending on firmware/mode — the file extension is not a reliable
    signal, so we look at the streams instead.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.debug("ffprobe failed for {}: {}", path, exc)
        return False
    streams = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(streams) != 2:
        return False
    if streams[0] != streams[1]:
        return False
    try:
        w, h = (int(x) for x in streams[0].split(","))
    except ValueError:
        return False
    return w == h and w >= 1920


def discover_recordings(mount_path: Path) -> list[InstaRecording]:
    """Scan an Insta360 SD card for video files and group into recordings.

    Looks in ``<mount_path>/DCIM/Camera01/`` for VID_*.insv and VID_*.mp4,
    groups by recording timestamp, and returns them sorted chronologically.
    Only ``_00_`` (back/main lens) segments are included.

    Each recording is probed with :func:`is_dual_fisheye` to decide
    whether it needs stitching — the X4 can write 360° captures as
    either ``.insv`` or ``.mp4``, so the file extension alone isn't a
    reliable signal.

    Args:
        mount_path: Root of the mounted SD card (e.g. ``/Volumes/Insta360 X4``).

    Returns:
        Sorted list of :class:`InstaRecording`, oldest first.
    """
    camera_dir = mount_path / "DCIM" / "Camera01"
    if not camera_dir.is_dir():
        logger.debug("No DCIM/Camera01 found at {}", mount_path)
        return []

    # Collect main-lens segments grouped by timestamp
    groups: dict[str, list[tuple[int, Path]]] = {}
    for f in camera_dir.iterdir():
        info = parse_insv_filename(f.name)
        if info is None or info.lens != "00":
            continue
        groups.setdefault(info.timestamp_str, []).append((info.segment, f))

    recordings: list[InstaRecording] = []
    for ts, segs in sorted(groups.items()):
        segs.sort(key=lambda t: t[0])  # sort by segment number
        paths = [p for _, p in segs]
        total = sum(p.stat().st_size for p in paths)
        # Probe the first segment — all segments of one recording share format
        needs_stitch = is_dual_fisheye(paths[0])
        recordings.append(
            InstaRecording(
                timestamp_str=ts,
                segments=paths,
                total_size_bytes=total,
                needs_stitching=needs_stitch,
            )
        )
        mode = "360°" if needs_stitch else "single-lens"
        logger.info(
            "Found recording {} — {} segment(s), {:.1f} MB ({})",
            ts,
            len(paths),
            total / 1_048_576,
            mode,
        )

    logger.info("Discovered {} recording(s) on {}", len(recordings), mount_path)
    return recordings


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def recording_start_utc(rec: InstaRecording, tz_name: str) -> datetime:
    """Convert a recording's filename timestamp to UTC.

    The camera stores timestamps in whatever timezone it was set to
    (typically the user's local time).  This function interprets the
    filename timestamp in the given timezone and returns UTC.

    Args:
        rec: Recording whose ``timestamp_str`` to convert.
        tz_name: IANA timezone name (e.g. ``"America/Los_Angeles"``).

    Returns:
        Timezone-aware UTC datetime.
    """
    tz = ZoneInfo(tz_name)
    naive = datetime.strptime(rec.timestamp_str, "%Y%m%d_%H%M%S")
    local_dt = naive.replace(tzinfo=tz)
    return local_dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Camera identity (volume UUID → user-assigned label)
# ---------------------------------------------------------------------------


def default_camera_labels_path() -> Path:
    """Return the on-disk camera-label config path on the Mac."""
    return Path.home() / ".config" / "helmlog" / "cameras.toml"


def volume_uuid_for(mount_path: Path) -> str | None:
    """Return the macOS volume UUID for a mounted volume.

    Uses ``diskutil info -plist <mount>``. Returns ``None`` on any
    failure (non-macOS, unmounted, etc.) so callers can fall back to
    the volume basename.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", str(mount_path)],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.debug("diskutil lookup failed for {}: {}", mount_path, exc)
        return None
    try:
        info = plistlib.loads(result.stdout)
    except plistlib.InvalidFileException as exc:
        logger.debug("Could not parse diskutil plist for {}: {}", mount_path, exc)
        return None
    uuid = info.get("VolumeUUID") or info.get("DiskUUID")
    return str(uuid) if uuid else None


def _placeholder_label(volume_uuid: str) -> str:
    """Build a placeholder camera label from a volume UUID."""
    short = volume_uuid.replace("-", "").lower()[:8]
    return f"camera-{short}"


def load_camera_labels(path: Path | None = None) -> dict[str, str]:
    """Load the volume-UUID → camera-label map from the Mac config file.

    Returns an empty dict if the file is missing or unparseable.
    """
    cfg = path or default_camera_labels_path()
    if not cfg.exists():
        return {}
    try:
        data = tomllib.loads(cfg.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Could not read camera labels {}: {}", cfg, exc)
        return {}
    cameras = data.get("cameras", {})
    if not isinstance(cameras, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in cameras.items() if v}


def save_camera_label(volume_uuid: str, label: str, path: Path | None = None) -> None:
    """Persist a label for a volume UUID, creating the file if needed."""
    cfg = path or default_camera_labels_path()
    existing = load_camera_labels(cfg)
    existing[volume_uuid.lower()] = label
    cfg.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[cameras]"]
    for uuid, lbl in sorted(existing.items()):
        lines.append(f'"{uuid}" = "{lbl}"')
    cfg.write_text("\n".join(lines) + "\n")


def resolve_camera_label(
    mount_path: Path,
    *,
    labels_path: Path | None = None,
) -> tuple[str, bool]:
    """Resolve the camera label to use for a mounted volume.

    Strategy:
      1. Look up the volume UUID via ``diskutil``.
      2. If a stored label exists for that UUID, use it (``known=True``).
      3. Otherwise persist a placeholder ``camera-<uuid8>`` and return
         it as ``known=False`` so the caller can notify the user to
         rename the camera in the config.
      4. If the UUID lookup fails, fall back to the volume basename.

    Returns:
        ``(label, is_user_assigned)``.
    """
    uuid = volume_uuid_for(mount_path)
    if uuid is None:
        fallback = mount_path.name or "camera"
        logger.warning(
            "No volume UUID for {} — using basename {!r} as camera label",
            mount_path,
            fallback,
        )
        return fallback, False

    labels = load_camera_labels(labels_path)
    stored = labels.get(uuid.lower())
    if stored:
        return stored, True

    placeholder = _placeholder_label(uuid)
    save_camera_label(uuid, placeholder, labels_path)
    logger.warning(
        "Unknown camera (volume UUID {}); using placeholder label {!r}. Edit {} to rename.",
        uuid,
        placeholder,
        labels_path or default_camera_labels_path(),
    )
    return placeholder, False


# ---------------------------------------------------------------------------
# Session matching
# ---------------------------------------------------------------------------


def match_sessions(
    rec_start: datetime,
    rec_end: datetime,
    sessions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the session with the most time overlap to a recording.

    Args:
        rec_start: Recording start time (UTC).
        rec_end: Recording end time (UTC).
        sessions: List of session dicts with ``start_utc``, ``end_utc`` keys
                  (ISO 8601 strings; ``end_utc`` may be ``None``).

    Returns:
        The best-matching session dict, or ``None`` if no overlap.
    """
    best: dict[str, Any] | None = None
    best_overlap = 0.0

    for s in sessions:
        s_start = datetime.fromisoformat(s["start_utc"])
        s_end = datetime.fromisoformat(s["end_utc"]) if s.get("end_utc") else rec_end

        overlap_start = max(rec_start, s_start)
        overlap_end = min(rec_end, s_end)
        overlap = (overlap_end - overlap_start).total_seconds()

        if overlap > best_overlap:
            best_overlap = overlap
            best = s

    if best_overlap <= 0:
        return None

    logger.info(
        "Matched recording to session {} ({:.0f}s overlap)",
        best.get("name", best.get("id")) if best else "?",
        best_overlap,
    )
    return best
