"""Unit tests for logger.cameras — OSC camera control and storage integration."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helmlog.cameras import (
    Camera,
    get_status,
    parse_cameras_config,
    start_all,
    start_camera,
    stop_all,
    stop_camera,
)

# ---------------------------------------------------------------------------
# setOptions called before startCapture (horizon metadata fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_camera_sends_set_options_then_start_capture() -> None:
    """start_camera must send setOptions(captureMode=video, videoStitching=none)
    followed by startCapture to produce .insv 360° recordings via OSC.

    A pre-flight getOptions call is also made for diagnostic logging; its
    failure is non-fatal and must not prevent recording from starting.

    Background: the X4 OSC layer is independent of the on-screen mode setting.
    Sending startCapture alone (no setOptions) defaults to single-lens mode and
    produces .mp4.  Both captureMode AND videoStitching must be set together —
    setting videoStitching alone without captureMode is silently ignored by the
    firmware (confirmed: still produces .mp4)."""
    cam = Camera(name="test", ip="192.168.42.1")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is True

    calls = mock_client.post.call_args_list
    sent_commands = [c.kwargs.get("json", {}).get("name") for c in calls]

    # Must include setOptions and startCapture (getOptions may also be present)
    assert "camera.setOptions" in sent_commands, f"camera.setOptions missing from {sent_commands}"
    assert "camera.startCapture" in sent_commands, (
        f"camera.startCapture missing from {sent_commands}"
    )

    set_options_idx = sent_commands.index("camera.setOptions")
    start_capture_idx = sent_commands.index("camera.startCapture")
    assert set_options_idx < start_capture_idx, "setOptions must come before startCapture"

    # Verify captureMode AND videoStitching are set together
    set_options_call = calls[set_options_idx]
    options = set_options_call.kwargs.get("json", {}).get("parameters", {}).get("options", {})
    assert options.get("captureMode") == "video", (
        "captureMode must be 'video' to put the OSC layer into video-recording mode"
    )
    assert options.get("videoStitching") == "none", (
        "videoStitching must be 'none' to request unstitched 360° .insv output"
    )


# ---------------------------------------------------------------------------
# parse_cameras_config
# ---------------------------------------------------------------------------


def test_parse_cameras_config_single() -> None:
    result = parse_cameras_config("main:192.168.8.50")
    assert len(result) == 1
    assert result[0].name == "main"
    assert result[0].ip == "192.168.8.50"
    assert result[0].model == "insta360-x4"


def test_parse_cameras_config_multiple() -> None:
    result = parse_cameras_config("port:192.168.8.50,starboard:192.168.8.51")
    assert len(result) == 2
    assert result[0].name == "port"
    assert result[1].name == "starboard"


def test_parse_cameras_config_empty() -> None:
    assert parse_cameras_config("") == []
    assert parse_cameras_config("  ") == []


def test_parse_cameras_config_whitespace() -> None:
    result = parse_cameras_config(" port : 192.168.8.50 , stern : 192.168.8.51 ")
    assert len(result) == 2
    assert result[0].name == "port"
    assert result[0].ip == "192.168.8.50"


def test_parse_cameras_config_invalid_entry() -> None:
    """Invalid entries (missing colon) are skipped."""
    result = parse_cameras_config("good:192.168.8.50,bad_no_colon")
    assert len(result) == 1
    assert result[0].name == "good"


# ---------------------------------------------------------------------------
# start_camera / stop_camera / get_status (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_camera_success() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is True
    assert status.error is None
    assert status.latency_ms is not None
    assert status.latency_ms >= 0


@pytest.mark.asyncio
async def test_start_camera_timeout() -> None:
    cam = Camera(name="slow", ip="192.168.8.50")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.TimeoutException("timed out")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=1.0)

    assert status.recording is False
    assert status.error is not None
    assert "timed out" in status.error


@pytest.mark.asyncio
async def test_start_camera_connection_error() -> None:
    cam = Camera(name="offline", ip="192.168.8.99")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is False
    assert "Connection refused" in (status.error or "")


@pytest.mark.asyncio
async def test_start_camera_empty_error_message() -> None:
    """httpx.ConnectError('') should produce a meaningful error, not empty string."""
    cam = Camera(name="unreachable", ip="192.168.42.1")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is False
    assert status.error  # must not be empty/None
    assert "unreachable" in status.error.lower() or "connect" in status.error.lower()


@pytest.mark.asyncio
async def test_stop_camera_empty_error_message() -> None:
    """stop_camera should also produce a meaningful error on empty exception."""
    cam = Camera(name="unreachable", ip="192.168.42.1")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await stop_camera(cam, timeout=5.0)

    assert status.recording is True  # assume still recording on failure
    assert status.error
    assert "unreachable" in status.error.lower() or "connect" in status.error.lower()


@pytest.mark.asyncio
async def test_stop_camera_success() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await stop_camera(cam, timeout=5.0)

    assert status.recording is False
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_recording() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"results": {"options": {"captureStatus": "shooting"}}}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is True
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_idle() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"results": {"options": {"captureStatus": "idle"}}}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is False
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_connection_error() -> None:
    cam = Camera(name="offline", ip="192.168.8.99")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("unreachable")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is False
    assert status.error is not None


# ---------------------------------------------------------------------------
# start_all / stop_all (with storage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_all_parallel(storage: object) -> None:
    """Two cameras, both succeed → two camera_sessions rows."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    # Create a race first
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 1, "test-race")
    race_id = race.id

    cams = [Camera(name="port", ip="1.1.1.1"), Camera(name="stern", ip="2.2.2.2")]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        statuses = await start_all(cams, race_id, storage)

    assert len(statuses) == 2
    assert all(s.recording for s in statuses)

    # Verify storage rows
    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 2
    assert {r["camera_name"] for r in rows} == {"port", "stern"}
    assert all(r["recording_started_utc"] is not None for r in rows)


@pytest.mark.asyncio
async def test_start_all_one_fails(storage: object) -> None:
    """First camera times out, second succeeds → partial success."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 2, "test-race-2")
    race_id = race.id

    cams = [Camera(name="broken", ip="1.1.1.1"), Camera(name="good", ip="2.2.2.2")]

    # broken camera raises, good camera works
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    call_count = 0

    async def _mock_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # Raise for any call targeting the "broken" camera's IP
        url = args[0] if args else kwargs.get("url", "")
        if "1.1.1.1" in str(url):
            raise httpx.ConnectError("Connection refused")
        return mock_resp

    mock_client = AsyncMock()
    mock_client.post = _mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        statuses = await start_all(cams, race_id, storage)

    assert len(statuses) == 2
    # One failed, one succeeded
    errors = [s for s in statuses if s.error]
    ok = [s for s in statuses if not s.error]
    assert len(errors) == 1
    assert len(ok) == 1

    # Both should have storage rows
    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_stop_all_updates_rows(storage: object) -> None:
    """stop_all updates camera_sessions with stopped_utc."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 3, "test-race-3")
    race_id = race.id

    cams = [Camera(name="main", ip="1.1.1.1")]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await start_all(cams, race_id, storage)
        await stop_all(cams, race_id, storage)

    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 1
    assert rows[0]["recording_stopped_utc"] is not None


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_camera_session_roundtrip(storage: object) -> None:
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 4, "test-race-4")
    race_id = race.id
    now = _dt.now(UTC)

    row_id = await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=42,
        error=None,
    )
    assert row_id > 0

    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 1
    assert rows[0]["camera_name"] == "bow"
    assert rows[0]["sync_offset_ms"] == 42
    assert rows[0]["error"] is None


@pytest.mark.asyncio
async def test_update_camera_session_stop(storage: object) -> None:
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 5, "test-race-5")
    race_id = race.id
    now = _dt.now(UTC)

    await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=10,
        error=None,
    )

    updated = await storage.update_camera_session_stop(
        session_id=race_id,
        camera_name="bow",
        stopped_utc=now,
        error=None,
    )
    assert updated is True

    rows = await storage.list_camera_sessions(race_id)
    assert rows[0]["recording_stopped_utc"] is not None


@pytest.mark.asyncio
async def test_list_unlinked_camera_sessions(storage: object) -> None:
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 6, "test-race-6")
    race_id = race.id
    now = _dt.now(UTC)

    await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=10,
        error=None,
    )

    unlinked = await storage.list_unlinked_camera_sessions()
    assert len(unlinked) >= 1
    assert any(r["camera_name"] == "bow" for r in unlinked)


# ---------------------------------------------------------------------------
# Camera config CRUD (storage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_camera_crud(storage: object) -> None:
    """Add, list, update, rename, and delete cameras in the DB."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)

    # Initially empty
    cams = await storage.list_cameras()
    assert cams == []

    # Add
    cam_id = await storage.add_camera("bow", "192.168.42.1")
    assert cam_id > 0
    cams = await storage.list_cameras()
    assert len(cams) == 1
    assert cams[0]["name"] == "bow"
    assert cams[0]["ip"] == "192.168.42.1"
    assert cams[0]["model"] == "insta360-x4"

    # Update IP
    ok = await storage.update_camera("bow", "10.0.0.1")
    assert ok is True
    cams = await storage.list_cameras()
    assert cams[0]["ip"] == "10.0.0.1"

    # Update IP + model
    ok = await storage.update_camera("bow", "10.0.0.2", model="gopro")
    assert ok is True
    cams = await storage.list_cameras()
    assert cams[0]["ip"] == "10.0.0.2"
    assert cams[0]["model"] == "gopro"

    # Rename
    ok = await storage.rename_camera("bow", "stern", "10.0.0.3")
    assert ok is True
    cams = await storage.list_cameras()
    assert cams[0]["name"] == "stern"

    # Delete
    ok = await storage.delete_camera("stern")
    assert ok is True
    cams = await storage.list_cameras()
    assert cams == []

    # Delete non-existent
    ok = await storage.delete_camera("nope")
    assert ok is False


@pytest.mark.asyncio
async def test_seed_cameras_from_env(storage: object) -> None:
    """Seed from CAMERAS env var string, but only when table is empty."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)

    count = await storage.seed_cameras_from_env("bow:192.168.42.1,stern:192.168.42.2")
    assert count == 2
    cams = await storage.list_cameras()
    assert len(cams) == 2

    # Seeding again should be a no-op (table not empty)
    count = await storage.seed_cameras_from_env("extra:10.0.0.1")
    assert count == 0
    cams = await storage.list_cameras()
    assert len(cams) == 2
