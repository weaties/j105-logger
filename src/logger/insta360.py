"""Insta360 / local video metadata extraction and race auto-association.

Reads video file metadata (creation time, duration) via ``ffprobe`` and
matches files to races by time-window overlap.  This module handles the
**metadata + linking** side — actual camera control (USB/BLE start/stop)
is out of scope.

Supported file extensions: ``.mp4``, ``.insv``, ``.lrv``
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from logger.races import Race

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

_DEFAULT_EXTENSIONS: tuple[str, ...] = (".mp4", ".insv", ".lrv")


@dataclass(frozen=True)
class LocalVideoFile:
    """Metadata extracted from a local video file."""

    file_path: str
    filename: str
    creation_utc: datetime
    duration_s: float
    end_utc: datetime = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "end_utc", self.creation_utc + timedelta(seconds=self.duration_s))


# ---------------------------------------------------------------------------
# Metadata extraction via ffprobe
# ---------------------------------------------------------------------------


async def extract_video_metadata(file_path: str) -> LocalVideoFile:
    """Extract creation time and duration from a video file using ``ffprobe``.

    Falls back to filesystem mtime if the container has no ``creation_time`` tag.
    Raises ``RuntimeError`` if ffprobe is not installed or the file is unreadable.
    """
    abs_path = str(Path(file_path).resolve())
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        abs_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        msg = stderr.decode().strip() or f"ffprobe exited with code {proc.returncode}"
        raise RuntimeError(f"ffprobe failed for {abs_path}: {msg}")

    data = json.loads(stdout)
    fmt = data.get("format", {})

    # Duration
    duration_s = float(fmt.get("duration", 0))

    # Creation time — try container metadata, fall back to filesystem mtime
    tags = fmt.get("tags", {})
    creation_str = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
    if creation_str:
        creation_utc = _parse_creation_time(creation_str)
    else:
        mtime = os.path.getmtime(abs_path)
        creation_utc = datetime.fromtimestamp(mtime, tz=UTC)
        logger.debug("No creation_time tag in {}; using mtime", abs_path)

    return LocalVideoFile(
        file_path=abs_path,
        filename=Path(abs_path).name,
        creation_utc=creation_utc,
        duration_s=duration_s,
    )


def _parse_creation_time(raw: str) -> datetime:
    """Parse a creation_time string from ffprobe into a UTC datetime.

    Handles ISO 8601 with or without timezone, and the common ffprobe format
    ``2026-03-01T13:45:00.000000Z``.
    """
    raw = raw.strip()
    # datetime.fromisoformat handles most formats in Python 3.12+
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------


async def scan_video_directory(
    directory: str,
    extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS,
) -> list[LocalVideoFile]:
    """Walk *directory* and extract metadata for each matching video file.

    Returns results sorted by ``creation_utc`` ascending.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Not a directory: {directory}")

    paths: list[str] = []
    for root, _dirs, files in os.walk(dir_path):
        for name in files:
            if Path(name).suffix.lower() in extensions:
                paths.append(os.path.join(root, name))

    if not paths:
        logger.info("No video files found in {} with extensions {}", directory, extensions)
        return []

    logger.info("Found {} video file(s) in {}", len(paths), directory)
    results = await asyncio.gather(
        *(extract_video_metadata(p) for p in paths),
        return_exceptions=True,
    )

    videos: list[LocalVideoFile] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("Skipping file: {}", r)
        else:
            videos.append(r)

    videos.sort(key=lambda v: v.creation_utc)
    return videos


# ---------------------------------------------------------------------------
# Race matching
# ---------------------------------------------------------------------------


def find_matching_races(video: LocalVideoFile, races: list[Race]) -> list[Race]:
    """Return races whose time window overlaps the video's.

    A race matches if ``race.start_utc < video.end_utc`` AND
    ``(race.end_utc is None OR race.end_utc > video.creation_utc)``.
    """
    matches: list[Race] = []
    for race in races:
        # Race hasn't started yet relative to video end — no overlap
        if race.start_utc >= video.end_utc:
            continue
        # Race ended before video started — no overlap
        if race.end_utc is not None and race.end_utc <= video.creation_utc:
            continue
        matches.append(race)
    return matches


def compute_sync_point(video: LocalVideoFile) -> tuple[datetime, float]:
    """Compute the sync point for a local video file.

    Returns ``(sync_utc, sync_offset_s)`` where sync_utc is the video's
    creation time and sync_offset_s is 0.0 (the video starts at t=0).
    """
    return video.creation_utc, 0.0
