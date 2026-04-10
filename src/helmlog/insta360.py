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

X4 OSC startCapture quirk
-------------------------
When the X4 is started via the OSC HTTP API (``camera.startCapture``)
rather than the physical shutter button, the camera writes a *correct*
dual-fisheye 360° recording — two HEVC video streams + audio + IMU
trailer — but **labels the file with a ``.mp4`` extension** instead of
``.insv``.  Insta360 Studio (and Pano2VR, the GoPro VR plugin, etc.)
gate 360° processing on the ``.insv`` extension and treat ``.mp4`` as
flat single-lens video, silently throwing away the second video stream
and the gyroscope data needed for horizon-leveling.

We work around this at scan time: each ``.mp4`` in the camera dir is
probed with ffprobe; if it has two video streams it is renamed on disk
to ``.insv`` so the rest of the pipeline (and external tools) sees it
as the 360° recording it really is.  Genuine single-lens ``.mp4`` files
(one video stream) are left untouched.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

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
# .mp4 → .insv promotion (see module docstring)
# ---------------------------------------------------------------------------


def _count_video_streams(path: Path) -> int | None:
    """Return the number of video streams in *path*, or ``None`` on probe failure.

    Uses ffprobe.  Returns ``None`` if ffprobe is not installed or the file
    cannot be parsed — callers should treat that as "unknown" and fall back
    to extension-based classification rather than mutating the file.
    """
    if shutil.which("ffprobe") is None:
        logger.warning("ffprobe not found on PATH; cannot probe {} for stream count", path.name)
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe failed on {}: {}", path.name, exc)
        return None
    if result.returncode != 0:
        logger.warning("ffprobe error on {}: {}", path.name, result.stderr.strip())
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def _promote_mp4_if_dual_stream(path: Path) -> Path:
    """If *path* is a dual-fisheye .mp4, rename it on disk to .insv.

    Returns the (possibly new) path.  No-op for files that are not ``.mp4``,
    have only one video stream, or whose stream count cannot be determined.
    Idempotent: if the .insv twin already exists, leaves *path* alone.
    """
    if path.suffix.lower() != ".mp4":
        return path
    streams = _count_video_streams(path)
    if streams is None or streams < 2:
        return path
    target = path.with_suffix(".insv")
    if target.exists():
        logger.warning("Refusing to promote {} → {}: target already exists", path.name, target.name)
        return path
    path.rename(target)
    logger.info(
        "Promoted X4 OSC recording {} → {} ({} video streams — dual-fisheye 360°)",
        path.name,
        target.name,
        streams,
    )
    return target


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_recordings(mount_path: Path) -> list[InstaRecording]:
    """Scan an Insta360 SD card for video files and group into recordings.

    Looks in ``<mount_path>/DCIM/Camera01/`` for VID_*.insv and VID_*.mp4,
    groups by recording timestamp, and returns them sorted chronologically.
    Only ``_00_`` (back/main lens) segments are included.

    Each ``.mp4`` is probed with ffprobe before grouping; if it contains two
    video streams it is renamed on disk to ``.insv`` (see module docstring
    on the X4 OSC startCapture quirk).

    Args:
        mount_path: Root of the mounted SD card (e.g. ``/Volumes/Insta360 X4``).

    Returns:
        Sorted list of :class:`InstaRecording`, oldest first.
    """
    camera_dir = mount_path / "DCIM" / "Camera01"
    if not camera_dir.is_dir():
        logger.debug("No DCIM/Camera01 found at {}", mount_path)
        return []

    # Collect main-lens segments grouped by timestamp + extension.
    # Dual-fisheye .mp4 files are promoted to .insv on disk during this pass.
    groups: dict[str, list[tuple[int, Path]]] = {}
    extensions: dict[str, str] = {}  # timestamp → extension
    for f in camera_dir.iterdir():
        info = parse_insv_filename(f.name)
        if info is None or info.lens != "00":
            continue
        if info.extension == "mp4":
            promoted = _promote_mp4_if_dual_stream(f)
            if promoted is not f:
                f = promoted
                info = parse_insv_filename(f.name)
                if info is None:
                    continue
        groups.setdefault(info.timestamp_str, []).append((info.segment, f))
        extensions[info.timestamp_str] = info.extension

    recordings: list[InstaRecording] = []
    for ts, segs in sorted(groups.items()):
        segs.sort(key=lambda t: t[0])  # sort by segment number
        paths = [p for _, p in segs]
        total = sum(p.stat().st_size for p in paths)
        ext = extensions[ts]
        needs_stitch = ext == "insv"
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
