"""Unit tests for logger.insta360 — video metadata extraction and race matching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from logger.insta360 import (
    LocalVideoFile,
    _parse_creation_time,
    compute_sync_point,
    extract_video_metadata,
    find_matching_races,
    scan_video_directory,
)

# ---------------------------------------------------------------------------
# Helpers — lightweight Race stand-in for pure-function tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeRace:
    """Minimal Race-compatible object for testing find_matching_races."""

    id: int
    name: str
    event: str
    race_num: int
    date: str
    start_utc: datetime
    end_utc: datetime | None
    session_type: str = "race"


def _race(
    *,
    start: datetime,
    end: datetime | None = None,
    race_id: int = 1,
    name: str = "test-race",
) -> _FakeRace:
    return _FakeRace(
        id=race_id,
        name=name,
        event="Test",
        race_num=1,
        date=start.strftime("%Y-%m-%d"),
        start_utc=start,
        end_utc=end,
    )


# ---------------------------------------------------------------------------
# LocalVideoFile dataclass
# ---------------------------------------------------------------------------


def test_local_video_file_end_utc() -> None:
    """end_utc is computed from creation_utc + duration_s."""
    creation = datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
    vid = LocalVideoFile(
        file_path="/tmp/test.mp4",
        filename="test.mp4",
        creation_utc=creation,
        duration_s=600.0,
    )
    assert vid.end_utc == creation + timedelta(seconds=600)


# ---------------------------------------------------------------------------
# _parse_creation_time
# ---------------------------------------------------------------------------


def test_parse_creation_time_iso_z() -> None:
    """Parse ISO 8601 with trailing Z."""
    dt = _parse_creation_time("2026-03-01T14:30:00.000000Z")
    assert dt == datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC)


def test_parse_creation_time_iso_offset() -> None:
    """Parse ISO 8601 with +00:00 offset."""
    dt = _parse_creation_time("2026-03-01T14:30:00+00:00")
    assert dt == datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC)


def test_parse_creation_time_no_tz() -> None:
    """Naive datetime gets UTC assumed."""
    dt = _parse_creation_time("2026-03-01T14:30:00")
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# extract_video_metadata
# ---------------------------------------------------------------------------

_FFPROBE_OUTPUT = {
    "format": {
        "duration": "1200.5",
        "tags": {
            "creation_time": "2026-03-01T14:00:00.000000Z",
        },
    }
}


@pytest.mark.asyncio
async def test_extract_video_metadata_success() -> None:
    """Parse ffprobe JSON output into LocalVideoFile."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (json.dumps(_FFPROBE_OUTPUT).encode(), b"")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        vid = await extract_video_metadata("/videos/race1.mp4")

    assert vid.filename == "race1.mp4"
    assert vid.duration_s == 1200.5
    assert vid.creation_utc == datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
    assert vid.end_utc == vid.creation_utc + timedelta(seconds=1200.5)


@pytest.mark.asyncio
async def test_extract_video_metadata_no_creation_time_falls_back_to_mtime() -> None:
    """When ffprobe has no creation_time tag, fall back to file mtime."""
    ffprobe_no_tag = {"format": {"duration": "300.0", "tags": {}}}
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (json.dumps(ffprobe_no_tag).encode(), b"")
    mock_proc.returncode = 0

    mtime = datetime(2026, 3, 1, 15, 0, 0, tzinfo=UTC).timestamp()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch("os.path.getmtime", return_value=mtime),
    ):
        vid = await extract_video_metadata("/videos/race2.mp4")

    assert vid.duration_s == 300.0
    assert vid.creation_utc.year == 2026
    assert vid.creation_utc.month == 3


@pytest.mark.asyncio
async def test_extract_video_metadata_ffprobe_fails() -> None:
    """ffprobe non-zero exit raises RuntimeError."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"No such file")
    mock_proc.returncode = 1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(RuntimeError, match="ffprobe failed"),
    ):
        await extract_video_metadata("/nonexistent.mp4")


# ---------------------------------------------------------------------------
# scan_video_directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_video_directory_filters_extensions(tmp_path: str) -> None:
    """Only .mp4, .insv, .lrv files are scanned."""
    import pathlib

    d = pathlib.Path(tmp_path)
    (d / "race.mp4").touch()
    (d / "race.insv").touch()
    (d / "notes.txt").touch()
    (d / "photo.jpg").touch()

    creation = datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)

    async def _mock_extract(path: str) -> LocalVideoFile:
        return LocalVideoFile(
            file_path=path,
            filename=pathlib.Path(path).name,
            creation_utc=creation,
            duration_s=100.0,
        )

    with patch("logger.insta360.extract_video_metadata", side_effect=_mock_extract):
        results = await scan_video_directory(str(d))

    filenames = {v.filename for v in results}
    assert "race.mp4" in filenames
    assert "race.insv" in filenames
    assert "notes.txt" not in filenames
    assert "photo.jpg" not in filenames


@pytest.mark.asyncio
async def test_scan_video_directory_not_a_dir() -> None:
    """Non-existent directory raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        await scan_video_directory("/nonexistent/path")


# ---------------------------------------------------------------------------
# find_matching_races
# ---------------------------------------------------------------------------


def test_find_matching_races_overlap() -> None:
    """Video that overlaps a race window matches."""
    vid = LocalVideoFile(
        file_path="/v/r.mp4",
        filename="r.mp4",
        creation_utc=datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC),
        duration_s=3600.0,
    )
    race = _race(
        start=datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC),
        end=datetime(2026, 3, 1, 15, 30, 0, tzinfo=UTC),
    )
    assert find_matching_races(vid, [race]) == [race]  # type: ignore[arg-type]


def test_find_matching_races_no_overlap() -> None:
    """Video and race that don't overlap → empty list."""
    vid = LocalVideoFile(
        file_path="/v/r.mp4",
        filename="r.mp4",
        creation_utc=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
        duration_s=1800.0,  # ends 10:30
    )
    race = _race(
        start=datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC),
        end=datetime(2026, 3, 1, 15, 0, 0, tzinfo=UTC),
    )
    assert find_matching_races(vid, [race]) == []  # type: ignore[arg-type]


def test_find_matching_races_open_race() -> None:
    """Race with end_utc=None (still running) matches if video starts after race start."""
    vid = LocalVideoFile(
        file_path="/v/r.mp4",
        filename="r.mp4",
        creation_utc=datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC),
        duration_s=600.0,
    )
    race = _race(
        start=datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC),
        end=None,
    )
    assert find_matching_races(vid, [race]) == [race]  # type: ignore[arg-type]


def test_find_matching_races_multiple() -> None:
    """Video spanning two races returns both."""
    vid = LocalVideoFile(
        file_path="/v/r.mp4",
        filename="r.mp4",
        creation_utc=datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC),
        duration_s=7200.0,  # 2 hours
    )
    r1 = _race(
        start=datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC),
        end=datetime(2026, 3, 1, 15, 0, 0, tzinfo=UTC),
        race_id=1,
        name="race-1",
    )
    r2 = _race(
        start=datetime(2026, 3, 1, 15, 30, 0, tzinfo=UTC),
        end=datetime(2026, 3, 1, 16, 0, 0, tzinfo=UTC),
        race_id=2,
        name="race-2",
    )
    matches = find_matching_races(vid, [r1, r2])  # type: ignore[arg-type]
    assert len(matches) == 2


# ---------------------------------------------------------------------------
# compute_sync_point
# ---------------------------------------------------------------------------


def test_compute_sync_point() -> None:
    """Sync point is video creation time with offset 0."""
    creation = datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
    vid = LocalVideoFile(
        file_path="/v/r.mp4",
        filename="r.mp4",
        creation_utc=creation,
        duration_s=600.0,
    )
    sync_utc, sync_offset = compute_sync_point(vid)
    assert sync_utc == creation
    assert sync_offset == 0.0
