"""Tests for src/logger/web.py — race API and audio integration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from logger.audio import AudioConfig, AudioDeviceNotFoundError, AudioSession
from logger.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WIND_DATA,
    COGSOGRecord,
    HeadingRecord,
    SpeedRecord,
    WindRecord,
)
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
async def test_instruments_returns_nulls_empty_db(storage: Storage) -> None:
    """GET /api/instruments returns all None values when no data is in the DB."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")

    assert resp.status_code == 200
    data = resp.json()
    assert data["heading_deg"] is None
    assert data["bsp_kts"] is None
    assert data["cog_deg"] is None
    assert data["sog_kts"] is None
    assert data["tws_kts"] is None
    assert data["twa_deg"] is None
    assert data["twd_deg"] is None
    assert data["aws_kts"] is None
    assert data["awa_deg"] is None


@pytest.mark.asyncio
async def test_instruments_returns_latest_values(storage: Storage) -> None:
    """GET /api/instruments returns correctly rounded values from each table."""
    ts = datetime(2026, 2, 26, 15, 0, 0, tzinfo=UTC)
    await storage.write(
        HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=ts,
            heading_deg=270.0,
            deviation_deg=None,
            variation_deg=None,
        )
    )
    await storage.write(
        SpeedRecord(pgn=PGN_SPEED_THROUGH_WATER, source_addr=5, timestamp=ts, speed_kts=6.5)
    )
    await storage.write(
        COGSOGRecord(pgn=PGN_COG_SOG_RAPID, source_addr=5, timestamp=ts, cog_deg=265.0, sog_kts=5.8)
    )
    await storage.write(
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=ts,
            wind_speed_kts=12.0,
            wind_angle_deg=45.0,
            reference=0,
        )
    )
    await storage.write(
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=ts,
            wind_speed_kts=14.5,
            wind_angle_deg=35.0,
            reference=2,
        )
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")

    assert resp.status_code == 200
    data = resp.json()
    assert data["heading_deg"] == 270.0
    assert data["bsp_kts"] == 6.5
    assert data["cog_deg"] == 265.0
    assert data["sog_kts"] == 5.8
    assert data["tws_kts"] == 12.0
    assert data["twa_deg"] == 45.0
    assert data["twd_deg"] == (270.0 + 45.0) % 360  # 315.0
    assert data["aws_kts"] == 14.5
    assert data["awa_deg"] == 35.0


@pytest.mark.asyncio
async def test_start_practice_creates_practice_session(storage: Storage) -> None:
    """POST /api/races/start?session_type=practice creates a practice session with P prefix."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start?session_type=practice")

    assert resp.status_code == 201
    data = resp.json()
    assert "P1" in data["name"]
    assert data["session_type"] == "practice"


@pytest.mark.asyncio
async def test_state_includes_next_practice_num(storage: Storage) -> None:
    """GET /api/state includes next_practice_num."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    assert "next_practice_num" in data
    assert data["next_practice_num"] == 1


@pytest.mark.asyncio
async def test_invalid_session_type_returns_422(storage: Storage) -> None:
    """POST /api/races/start with invalid session_type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start?session_type=invalid")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_index_substitutes_grafana_url(storage: Storage) -> None:
    """GET / returns HTML with Grafana placeholders replaced by the configured URL/UID."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    html = resp.text
    assert "__GRAFANA_URL__" not in html
    assert "__GRAFANA_UID__" not in html
    # Default values are present
    assert "http://corvopi:3001" in html
    assert "j105-sailing" in html


@pytest.mark.asyncio
async def test_index_uses_env_grafana_url(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET / uses GRAFANA_URL / GRAFANA_DASHBOARD_UID env vars when set."""
    monkeypatch.setenv("GRAFANA_URL", "http://myhost:3001")
    monkeypatch.setenv("GRAFANA_DASHBOARD_UID", "custom-uid")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    html = resp.text
    assert "http://myhost:3001" in html
    assert "custom-uid" in html
    assert "__GRAFANA_URL__" not in html


@pytest.mark.asyncio
async def test_end_race_no_active_recording_is_noop(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/end does not call recorder.stop() if no recording started."""
    # Start a race without a recorder (so _audio_session_id stays None), then
    # end it with a recorder attached — stop() should NOT be called.
    recorder = _make_recorder()
    no_recorder_app = create_app(storage)
    recorder_app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )

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
