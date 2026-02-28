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


# ---------------------------------------------------------------------------
# Tests — debrief mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debrief_start_creates_audio(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/debrief/start calls recorder.start() with name containing '-debrief'."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")

    assert debrief_resp.status_code == 201
    # recorder.start called twice: once for race, once for debrief
    assert recorder.start.await_count == 2
    _args, kwargs = recorder.start.await_args
    assert "name" in kwargs
    assert kwargs["name"].endswith("-debrief")


@pytest.mark.asyncio
async def test_debrief_stop_ends_audio(storage: Storage, tmp_path: Path) -> None:
    """POST /api/debrief/stop calls recorder.stop() and returns 204."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        await client.post(f"/api/races/{race_id}/debrief/start")
        stop_resp = await client.post("/api/debrief/stop")

    assert stop_resp.status_code == 204
    # stop called twice: once for race end, once for debrief stop
    assert recorder.stop.await_count == 2


@pytest.mark.asyncio
async def test_debrief_on_open_race_auto_ends_it(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/debrief/start on an in-progress race auto-ends the race first."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        # Race is still in progress — debrief auto-ends it
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")

    assert debrief_resp.status_code == 201


@pytest.mark.asyncio
async def test_debrief_no_recorder_returns_409(storage: Storage) -> None:
    """POST /api/races/{id}/debrief/start with no recorder configured returns 409."""
    app = create_app(storage)  # no recorder

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Use a dummy race_id — the recorder check happens first
        debrief_resp = await client.post("/api/races/1/debrief/start")

    assert debrief_resp.status_code == 409


@pytest.mark.asyncio
async def test_state_includes_debrief_fields(storage: Storage, tmp_path: Path) -> None:
    """GET /api/state returns has_recorder and current_debrief fields."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app_with = create_app(storage, recorder=recorder, audio_config=config)
    app_without = create_app(storage)

    # Without recorder: has_recorder false, current_debrief null
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_without), base_url="http://test"
    ) as client:
        data = (await client.get("/api/state")).json()
    assert data["has_recorder"] is False
    assert data["current_debrief"] is None

    # With recorder, before debrief: has_recorder true, current_debrief null
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with), base_url="http://test"
    ) as client:
        data = (await client.get("/api/state")).json()
        assert data["has_recorder"] is True
        assert data["current_debrief"] is None

        # Start a race, end it, then start debrief
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        await client.post(f"/api/races/{race_id}/debrief/start")

        data = (await client.get("/api/state")).json()
    assert data["has_recorder"] is True
    assert data["current_debrief"] is not None
    assert data["current_debrief"]["race_id"] == race_id
    assert "race_name" in data["current_debrief"]
    assert "start_utc" in data["current_debrief"]


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


# ---------------------------------------------------------------------------
# /history page and /api/sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_page_served(storage: Storage) -> None:
    """GET /history returns the history HTML page."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/history")
    assert resp.status_code == 200
    assert "Session History" in resp.text
    assert "/api/sessions" in resp.text


@pytest.mark.asyncio
async def test_main_page_has_history_link(storage: Storage) -> None:
    """GET / contains a link to /history."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "/history" in resp.text


@pytest.mark.asyncio
async def test_api_sessions_empty(storage: Storage) -> None:
    """GET /api/sessions returns empty list when no sessions exist."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["sessions"] == []


@pytest.mark.asyncio
async def test_api_sessions_returns_races_and_practices(storage: Storage) -> None:
    """GET /api/sessions returns all races and practices sorted newest-first."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Create a race and a practice
        r1 = (await client.post("/api/races/start?session_type=race")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        r2 = (await client.post("/api/races/start?session_type=practice")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 2
    types = [s["type"] for s in data["sessions"]]
    assert "race" in types
    assert "practice" in types


@pytest.mark.asyncio
async def test_api_sessions_type_filter(storage: Storage) -> None:
    """GET /api/sessions?type=race only returns races."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start?session_type=race")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        r2 = (await client.post("/api/races/start?session_type=practice")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp = await client.get("/api/sessions?type=race")
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["type"] == "race"


@pytest.mark.asyncio
async def test_api_sessions_invalid_type(storage: Storage) -> None:
    """GET /api/sessions?type=bogus returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions?type=bogus")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_sessions_search(storage: Storage) -> None:
    """GET /api/sessions?q=BallardCup only returns sessions matching the query."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client, "BallardCup")
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")

        # Set a different event and create another race
        await _set_event(client, "CYC")
        r2 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp_all = await client.get("/api/sessions")
        resp_filtered = await client.get("/api/sessions?q=BallardCup")

    assert resp_all.json()["total"] == 2
    data = resp_filtered.json()
    assert data["total"] == 1
    assert "BallardCup" in data["sessions"][0]["event"]


@pytest.mark.asyncio
async def test_api_sessions_date_filter(storage: Storage) -> None:
    """GET /api/sessions?from_date=...&to_date=... filters by date."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")

        today = datetime.now(UTC).date().isoformat()
        resp_in = await client.get(f"/api/sessions?from_date={today}&to_date={today}")
        resp_out = await client.get("/api/sessions?from_date=2000-01-01&to_date=2000-01-02")

    assert resp_in.json()["total"] == 1
    assert resp_out.json()["total"] == 0


@pytest.mark.asyncio
async def test_api_sessions_pagination(storage: Storage) -> None:
    """GET /api/sessions limit/offset pagination works correctly."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Create 3 races
        for _ in range(3):
            r = (await client.post("/api/races/start")).json()
            await client.post(f"/api/races/{r['id']}/end")

        resp_all = await client.get("/api/sessions?limit=10")
        resp_page1 = await client.get("/api/sessions?limit=2&offset=0")
        resp_page2 = await client.get("/api/sessions?limit=2&offset=2")

    assert resp_all.json()["total"] == 3
    assert len(resp_page1.json()["sessions"]) == 2
    assert len(resp_page2.json()["sessions"]) == 1


@pytest.mark.asyncio
async def test_api_sessions_has_audio_flag(storage: Storage, tmp_path: Path) -> None:
    """has_audio is True for a race that has an associated audio session."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 1
    s = data["sessions"][0]
    assert s["has_audio"] is True
    assert s["audio_session_id"] is not None


@pytest.mark.asyncio
async def test_api_sessions_includes_crew(storage: Storage) -> None:
    """GET /api/sessions returns crew list per race/practice session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        race_id = r["id"]
        # Set crew for the race
        crew_payload = [
            {"position": "helm", "sailor": "Mark"},
            {"position": "main", "sailor": "Dave"},
        ]
        await client.post(
            f"/api/races/{race_id}/crew",
            json=crew_payload,
        )
        await client.post(f"/api/races/{race_id}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 1
    session = data["sessions"][0]
    assert "crew" in session
    pos_map = {c["position"]: c["sailor"] for c in session["crew"]}
    assert pos_map.get("helm") == "Mark"
    assert pos_map.get("main") == "Dave"


@pytest.mark.asyncio
async def test_api_sessions_includes_debriefs(storage: Storage, tmp_path: Path) -> None:
    """Completed debriefs appear as separate 'debrief' rows in /api/sessions."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")
        await client.post(f"/api/races/{r['id']}/debrief/start")
        await client.post("/api/debrief/stop")

        resp_all = await client.get("/api/sessions")
        resp_debrief = await client.get("/api/sessions?type=debrief")

    all_data = resp_all.json()
    types = [s["type"] for s in all_data["sessions"]]
    assert "debrief" in types

    deb_data = resp_debrief.json()
    assert deb_data["total"] == 1
    deb = deb_data["sessions"][0]
    assert deb["type"] == "debrief"
    assert deb["parent_race_id"] == r["id"]
    assert deb["has_audio"] is True


# ---------------------------------------------------------------------------
# Crew API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_crew_sets_crew(storage: Storage) -> None:
    """POST /api/races/{id}/crew then GET returns the same crew in canonical order."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]

        post_resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position": "helm", "sailor": "Mark"},
                {"position": "main", "sailor": "Dave"},
                {"position": "tactician", "sailor": "Bill"},
            ],
        )
        assert post_resp.status_code == 204

        get_resp = await client.get(f"/api/races/{race_id}/crew")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert "crew" in data
    assert "recent_sailors" in data
    positions = [c["position"] for c in data["crew"]]
    assert positions == ["helm", "main", "tactician"]
    sailors = {c["position"]: c["sailor"] for c in data["crew"]}
    assert sailors["helm"] == "Mark"
    assert sailors["main"] == "Dave"
    assert sailors["tactician"] == "Bill"


@pytest.mark.asyncio
async def test_post_crew_invalid_position(storage: Storage) -> None:
    """POST /api/races/{id}/crew with an unknown position returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[{"position": "captain", "sailor": "Someone"}],
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_crew_unknown_race(storage: Storage) -> None:
    """GET /api/races/{id}/crew for a non-existent race returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/races/99999/crew")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_state_includes_crew(storage: Storage) -> None:
    """/api/state today_races include crew list per race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[{"position": "helm", "sailor": "TestHelm"}],
        )

        state = (await client.get("/api/state")).json()

    assert state["current_race"] is not None
    assert "crew" in state["current_race"]
    sailors = {c["position"]: c["sailor"] for c in state["current_race"]["crew"]}
    assert sailors.get("helm") == "TestHelm"

    # today_races also includes crew
    assert len(state["today_races"]) == 1
    assert "crew" in state["today_races"][0]


@pytest.mark.asyncio
async def test_recent_sailors_endpoint(storage: Storage) -> None:
    """GET /api/sailors/recent returns names after crew is set."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position": "helm", "sailor": "Alice"},
                {"position": "main", "sailor": "Bob"},
            ],
        )

        resp = await client.get("/api/sailors/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert "sailors" in data
    assert "Alice" in data["sailors"]
    assert "Bob" in data["sailors"]


@pytest.mark.asyncio
async def test_post_crew_ignores_blank_sailors(storage: Storage) -> None:
    """POST /api/races/{id}/crew skips entries with blank sailor names."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position": "helm", "sailor": "Mark"},
                {"position": "main", "sailor": ""},
                {"position": "pit", "sailor": "  "},
            ],
        )

        resp = await client.get(f"/api/races/{race_id}/crew")
    crew = resp.json()["crew"]
    positions = [c["position"] for c in crew]
    assert "helm" in positions
    assert "main" not in positions
    assert "pit" not in positions


# ---------------------------------------------------------------------------
# Issue #30: debrief auto-stop + crew carry-forward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_while_debrief_active_stops_debrief(
    storage: Storage, tmp_path: Path
) -> None:
    """Starting a race while a debrief is active auto-stops the debrief."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Start + end race 1, then start a debrief
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        await client.post(f"/api/races/{r1['id']}/debrief/start")

        # Verify debrief is active
        state = (await client.get("/api/state")).json()
        assert state["current_debrief"] is not None

        # Start race 2 without explicitly stopping the debrief
        r2 = await client.post("/api/races/start")
        assert r2.status_code == 201

        # Debrief should have been auto-stopped
        state = (await client.get("/api/state")).json()
        assert state["current_debrief"] is None

    # recorder.stop() called: once for race 1 end, once for debrief auto-stop
    assert recorder.stop.await_count == 2


@pytest.mark.asyncio
async def test_debrief_end_utc_written_when_race_starts(storage: Storage, tmp_path: Path) -> None:
    """When a race starts auto-stopping a debrief, end_utc is persisted to the DB."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        await client.post(f"/api/races/{r1['id']}/debrief/start")

        # Start race 2 — should auto-stop the debrief
        await client.post("/api/races/start")

    # Check that the debrief audio_session row has end_utc set
    db = storage._conn()
    cur = await db.execute(
        "SELECT end_utc FROM audio_sessions WHERE session_type = 'debrief' ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["end_utc"] is not None


@pytest.mark.asyncio
async def test_debrief_auto_ends_open_race(storage: Storage, tmp_path: Path) -> None:
    """Starting a debrief on an in-progress race auto-ends the race first (defensive AC #2)."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        race_id = r["id"]

        # Start debrief without ending the race first
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")
        assert debrief_resp.status_code == 201

    # Race recording should have been stopped (once for the auto-end)
    # Debrief recording starts after, so recorder.start called twice total
    assert recorder.start.await_count == 2

    # The race row should now have end_utc set
    db = storage._conn()
    cur = await db.execute("SELECT end_utc FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    assert row is not None
    assert row["end_utc"] is not None


@pytest.mark.asyncio
async def test_start_race_carries_forward_crew(storage: Storage) -> None:
    """Starting a new race copies crew from the most recently ended session as defaults."""
    app = create_app(storage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)

        # Race 1: set crew, then end it
        r1 = (await client.post("/api/races/start")).json()
        await client.post(
            f"/api/races/{r1['id']}/crew",
            json=[
                {"position": "helm", "sailor": "Alice"},
                {"position": "main", "sailor": "Bob"},
            ],
        )
        await client.post(f"/api/races/{r1['id']}/end")

        # Race 2: start without posting any crew
        r2 = (await client.post("/api/races/start")).json()

        # Crew should have been carried forward from race 1
        crew_resp = await client.get(f"/api/races/{r2['id']}/crew")

    assert crew_resp.status_code == 200
    crew = crew_resp.json()["crew"]
    pos_map = {c["position"]: c["sailor"] for c in crew}
    assert pos_map.get("helm") == "Alice"
    assert pos_map.get("main") == "Bob"


# ---------------------------------------------------------------------------
# Session notes API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_note_returns_201(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes creates a note and returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "Upwind leg, 15kts TWS", "note_type": "text"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "ts" in data


@pytest.mark.asyncio
async def test_create_note_blank_body_returns_422(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with a blank body returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "   ", "note_type": "text"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_note_unknown_session_returns_404(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes for a non-existent session returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/9999/notes",
            json={"body": "note", "note_type": "text"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_notes_returns_notes(storage: Storage) -> None:
    """GET /api/sessions/{id}/notes returns notes for the session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(
            f"/api/sessions/{race_id}/notes", json={"body": "Note one"}
        )
        await client.post(
            f"/api/sessions/{race_id}/notes", json={"body": "Note two"}
        )
        resp = await client.get(f"/api/sessions/{race_id}/notes")
    assert resp.status_code == 200
    notes = resp.json()
    assert len(notes) == 2
    bodies = [n["body"] for n in notes]
    assert "Note one" in bodies
    assert "Note two" in bodies


@pytest.mark.asyncio
async def test_delete_note_returns_204(storage: Storage) -> None:
    """DELETE /api/notes/{id} returns 204 and the note is gone."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        create_resp = await client.post(
            f"/api/sessions/{race_id}/notes", json={"body": "To delete"}
        )
        note_id = create_resp.json()["id"]
        del_resp = await client.delete(f"/api/notes/{note_id}")
        list_resp = await client.get(f"/api/sessions/{race_id}/notes")
    assert del_resp.status_code == 204
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_delete_note_not_found_returns_404(storage: Storage) -> None:
    """DELETE /api/notes/{id} for a missing note returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/notes/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_grafana_annotations_returns_list(storage: Storage) -> None:
    """GET /api/grafana/annotations returns annotation objects."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(
            f"/api/sessions/{race_id}/notes", json={"body": "Tack at mark"}
        )
        from_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        to_ms = int(datetime(2026, 12, 31, tzinfo=UTC).timestamp() * 1000)
        resp = await client.get(
            f"/api/grafana/annotations?from={from_ms}&to={to_ms}"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "time" in data[0]
    assert "text" in data[0]
    assert "tags" in data[0]
    assert data[0]["text"] == "Tack at mark"


# ---------------------------------------------------------------------------
# Phase 2 notes: settings, photo, serve, traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_settings_note_returns_201(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with note_type='settings' and valid JSON body returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": '{"TWS": "15", "TWD": "220"}', "note_type": "settings"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "ts" in data


@pytest.mark.asyncio
async def test_create_settings_note_invalid_json_returns_422(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with note_type='settings' and non-JSON body returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "not valid json", "note_type": "settings"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_photo_note_returns_201(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/sessions/{id}/notes/photo with a file creates a photo note and saves file."""
    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes/photo",
            files={"file": ("test.jpg", jpeg_bytes, "image/jpeg")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "photo_path" in data
    # File should exist on disk
    assert (notes_dir / data["photo_path"]).exists()


@pytest.mark.asyncio
async def test_serve_note_photo_returns_200(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} serves a file that exists in NOTES_DIR."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    photo_file = notes_dir / "test.jpg"
    photo_file.write_bytes(b"\xff\xd8\xff\xe0test")

    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/notes/test.jpg")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_serve_note_photo_traversal_blocked(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path traversal via URL-encoded dots is blocked with 403 (not served)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Use URL-encoded dots to bypass client-side normalization
        resp = await client.get("/notes/%2e%2e/%2e%2e/etc/passwd")
    # Path traversal must not return 200 — either 403 (blocked) or 404 (not found)
    assert resp.status_code in (403, 404)
