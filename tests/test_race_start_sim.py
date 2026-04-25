"""Tests for the race-start simulator (#690).

Covers:
- The simulator is 404 unless RACE_START_SIMULATOR=true.
- Setting the virtual clock offset shifts the FSM clock so flag transitions
  fire deterministically without waiting real time.
- Synthetic boat-state writes feed line-metrics and ping fallback.
- Scenario presets apply offset + boat state in one call.
- The simulator does not leak into production (router not mounted).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim_env() -> dict[str, str]:
    return {"RACE_START_SIMULATOR": "true"}


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_routes_404_without_env(storage: Storage) -> None:
    """Simulator routes are not mounted when RACE_START_SIMULATOR is unset."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RACE_START_SIMULATOR", None)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_sim_routes_mounted_with_env(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 200
        assert r.json()["offset_s"] == 0.0


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_offset_persists_on_app_state(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post("/api/race-start/sim/clock", json={"offset_s": -300.0})
            assert r.status_code == 200
            r = await client.get("/api/race-start/sim/clock")
        assert r.json()["offset_s"] == -300.0
        assert app.state.race_start_sim_offset_s == -300.0


@pytest.mark.asyncio
async def test_offset_skews_fsm_clock(storage: Storage) -> None:
    """Arming with t0 = wall_now + 60s and then offsetting +120s should
    make the FSM see itself as past t0 — phase advances to 'started'."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
            await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
            # Without offset, FSM is in counting_down.
            r = await client.get("/api/race-start/state")
            assert r.json()["phase"] == "counting_down"

            # Skew +120s — virtual now > t0 → started.
            await client.post("/api/race-start/sim/clock", json={"offset_s": 120.0})
            r = await client.get("/api/race-start/state")
        assert r.json()["phase"] == "started"


# ---------------------------------------------------------------------------
# Synthetic boat state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_boat_writes_position(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/boat",
                json={
                    "latitude_deg": 47.65,
                    "longitude_deg": -122.40,
                    "sog_kn": 5.0,
                    "cog_deg": 0.0,
                    "twd_deg": 180.0,
                    "tws_kn": 10.0,
                },
            )
        assert r.status_code == 200
        written = set(r.json()["written"])
        assert {"position", "cogsog", "wind"}.issubset(written)

        latest = await storage.latest_position()
        assert latest is not None
        assert latest["latitude_deg"] == pytest.approx(47.65)


@pytest.mark.asyncio
async def test_sim_boat_partial_payload(storage: Storage) -> None:
    """Only fields supplied get written; unspecified ones are skipped."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
        assert r.json()["written"] == ["position"]


@pytest.mark.asyncio
async def test_sim_boat_then_ping_uses_db_position(storage: Storage) -> None:
    """Simulator-written position satisfies the real ping endpoint's
    DB fallback — closes the loop end-to-end."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/race-start/sim/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
            r = await client.post("/api/race-start/ping/boat", json={})
        assert r.status_code == 200
        assert r.json()["start_line"]["boat_end_lat"] == pytest.approx(47.65)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenarios_listed(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/scenarios")
        names = [s["name"] for s in r.json()["scenarios"]]
        assert "boat-favoured-square-line" in names
        assert "pin-favoured-3-2-1-0" in names
        assert "general-recall-at-minus-30" in names
        assert "ocs-at-plus-2" in names


@pytest.mark.asyncio
async def test_step_unknown_scenario_404(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "nope", "step_index": 0},
            )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_step_out_of_range_400(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "boat-favoured-square-line", "step_index": 99},
            )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_step_applies_offset_and_boat(storage: Storage) -> None:
    """Stepping a scenario sets the offset and writes synthetic state."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "pin-favoured-3-2-1-0", "step_index": 0},
            )
            data = r.json()
            assert data["offset_s"] == -180
            assert "position" in data["boat_written"]
            # Wind row should be present in the DB now.
            wind_row = await storage._conn().execute(  # noqa: SLF001
                "SELECT wind_angle_deg FROM winds ORDER BY id DESC LIMIT 1"
            )
            row = await wind_row.fetchone()
            assert row is not None
            assert row[0] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_clears_offset_and_fsm(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
            await client.post("/api/race-start/sim/clock", json={"offset_s": -200.0})
            r = await client.post("/api/race-start/sim/reset")
            assert r.status_code == 200

            # Offset back to 0
            assert app.state.race_start_sim_offset_s == 0.0
            # FSM cleared
            r = await client.get("/api/race-start/state")
        assert r.json()["phase"] == "idle"


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_page_renders(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/race-start/simulate")
        assert r.status_code == 200
        assert "Simulator" in r.text


@pytest.mark.asyncio
async def test_sim_page_404_without_env(storage: Storage) -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RACE_START_SIMULATOR", None)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/race-start/simulate")
        assert r.status_code == 404
