"""Tests for src/logger/web.py — race API and audio integration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from logger.audio import AudioConfig, AudioDeviceNotFoundError, AudioSession
from logger.web import create_app

if TYPE_CHECKING:
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEVICE = "Gordik 2T1R USB Audio"
_START_UTC = datetime(2026, 2, 26, 14, 0, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 2, 26, 14, 30, 0, tzinfo=UTC)


def _make_session(*, end_utc: datetime | None = None) -> AudioSession:
    return AudioSession(
        file_path="/tmp/audio/20260226-TestRegatta-1.wav",
        device_name=_DEVICE,
        start_utc=_START_UTC,
        end_utc=end_utc,
        sample_rate=48000,
        channels=1,
    )


def _make_recorder(*, raises_on_start: bool = False) -> MagicMock:
    """Return a mock AudioRecorder with async start/stop."""
    recorder = MagicMock()
    if raises_on_start:
        recorder.start = AsyncMock(side_effect=AudioDeviceNotFoundError("no device"))
    else:
        recorder.start = AsyncMock(return_value=_make_session())
        recorder.stop = AsyncMock(return_value=_make_session(end_utc=_END_UTC))
    return recorder


async def _set_event(client: httpx.AsyncClient, name: str = "TestRegatta") -> None:
    resp = await client.post("/api/event", json={"event_name": name})
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Tests — basic API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_endpoint_returns_json(storage: Storage) -> None:
    """GET /api/state returns a valid JSON response."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    assert "date" in data
    assert "current_race" in data
    assert "today_races" in data


@pytest.mark.asyncio
async def test_start_race_no_event_returns_422(storage: Storage) -> None:
    """POST /api/races/start fails with 422 when no event is configured."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/races/start")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_event_and_start_race(storage: Storage) -> None:
    """POST /api/event + POST /api/races/start creates a race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "TestRegatta" in data["name"]
    assert data["race_num"] == 1


# ---------------------------------------------------------------------------
# Tests — audio integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_triggers_audio_start(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/start calls recorder.start() with the race name."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    recorder.start.assert_awaited_once()
    _args, kwargs = recorder.start.await_args
    assert "name" in kwargs
    assert "TestRegatta" in kwargs["name"]


@pytest.mark.asyncio
async def test_end_race_triggers_audio_stop(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/end calls recorder.stop() and updates the DB."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        end_resp = await client.post(f"/api/races/{race_id}/end")

    assert end_resp.status_code == 204
    recorder.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_race_audio_device_not_found(storage: Storage, tmp_path: Path) -> None:
    """AudioDeviceNotFoundError is caught; race is still created successfully."""
    recorder = _make_recorder(raises_on_start=True)
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    assert "name" in resp.json()


@pytest.mark.asyncio
async def test_start_race_without_recorder(storage: Storage) -> None:
    """create_app without a recorder still creates races normally."""
    app = create_app(storage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_end_race_no_active_recording_is_noop(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/end does not call recorder.stop() if no recording started."""
    # Start a race without a recorder (so _audio_session_id stays None), then
    # end it with a recorder attached — stop() should NOT be called.
    recorder = _make_recorder()
    no_recorder_app = create_app(storage)
    recorder_app = create_app(storage, recorder=recorder,
                               audio_config=AudioConfig(device=None, sample_rate=48000,
                                                        channels=1, output_dir=str(tmp_path)))

    # Use the no-recorder app to start a race
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=no_recorder_app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]

    # Use the recorder app to end it — _audio_session_id is None so stop() is skipped
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorder_app), base_url="http://test"
    ) as client:
        end_resp = await client.post(f"/api/races/{race_id}/end")

    assert end_resp.status_code == 204
    recorder.stop.assert_not_called()
