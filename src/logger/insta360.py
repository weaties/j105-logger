"""Insta360 X4 file discovery and recording grouping.

Scans an SD card mount point for .insv video files, parses their
timestamp-based filenames, and groups them into logical recordings
that can be stitched into equirectangular 360° video.

File naming convention (Insta360 X4):
    VID_YYYYMMDD_HHMMSS_XX_NNN.insv
      XX  = lens: 00 (back), 10 (front), 01/11 (LRV preview)
      NNN = segment number (000, 001, ...)

Only back-lens (_00_) .insv files are included in recording segments;
the stitcher (insta360-cli-utils) automatically pairs front+back.
"""

from __future__ import annotations

import re
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

# Matches: VID_YYYYMMDD_HHMMSS_XX_NNN.insv  (also PRO_VID_ prefix)
_INSV_RE = re.compile(r"^(?:PRO_)?VID_(\d{8}_\d{6})_(\d{2})_(\d{3})\.insv$")


@dataclass(frozen=True)
class InsvFileInfo:
    """Parsed metadata from an .insv filename."""

    timestamp_str: str  # YYYYMMDD_HHMMSS
    lens: str  # 00 = back, 10 = front
    segment: int  # 0-based segment number


def parse_insv_filename(name: str) -> InsvFileInfo | None:
    """Parse an Insta360 .insv filename into structured metadata.

    Returns None for non-VID files (LRV previews, photos, etc.).
    """
    m = _INSV_RE.match(name)
    if m is None:
        return None
    return InsvFileInfo(
        timestamp_str=m.group(1),
        lens=m.group(2),
        segment=int(m.group(3)),
    )


# ---------------------------------------------------------------------------
# Recording dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstaRecording:
    """A single continuous recording (may span multiple segments).

    Attributes:
        timestamp_str: Recording start time from filename (YYYYMMDD_HHMMSS).
        segments: Ordered list of back-lens .insv file paths.
        total_size_bytes: Combined size of all segment files.
    """

    timestamp_str: str
    segments: list[Path] = field(default_factory=list)
    total_size_bytes: int = 0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_recordings(mount_path: Path) -> list[InstaRecording]:
    """Scan an Insta360 SD card for .insv files and group into recordings.

    Looks in ``<mount_path>/DCIM/Camera01/`` for VID_*.insv files,
    groups by recording timestamp, and returns them sorted chronologically.
    Only back-lens (``_00_``) segments are included; the stitcher pairs
    front+back automatically.

    Args:
        mount_path: Root of the mounted SD card (e.g. ``/Volumes/Insta360 X4``).

    Returns:
        Sorted list of :class:`InstaRecording`, oldest first.
    """
    camera_dir = mount_path / "DCIM" / "Camera01"
    if not camera_dir.is_dir():
        logger.debug("No DCIM/Camera01 found at {}", mount_path)
        return []

    # Collect back-lens segments grouped by timestamp
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
        recordings.append(InstaRecording(timestamp_str=ts, segments=paths, total_size_bytes=total))
        logger.info(
            "Found recording {} — {} segment(s), {:.1f} MB",
            ts,
            len(paths),
            total / 1_048_576,
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
