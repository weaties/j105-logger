"""Web tests for race-start routes (#644).

Covers:
- /race-start page rendering (viewer)
- /api/race-start/state happy path + auth gating
- mutation endpoints (arm/sync/nudge/postpone/recall/restart/abandon/reset)
- ping endpoints + line metrics
- viewer is blocked from mutations (403)
- viewer can read state (200)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


T0 = datetime(2026, 5, 1, 13, 45, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_viewer(storage: Storage) -> str:
    user_id = await storage.create_user("viewer-rs@test.com", "Viewer", "viewer")
    sid = generate_token()
    await storage.create_session(sid, user_id, session_expires_at())
    return sid


async def _create_crew(storage: Storage) -> str:
    user_id = await storage.create_user("crew-rs@test.com", "Crew", "crew")
    sid = generate_token()
    await storage.create_session(sid, user_id, session_expires_at())
    return sid


# ---------------------------------------------------------------------------
# AUTH_DISABLED=true (mock admin) — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_renders(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/race-start")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Class flag" in resp.text


@pytest.mark.asyncio
async def test_state_returns_idle_initially(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/race-start/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "idle"
    assert body["t0_utc"] is None
    assert body["start_line"]["is_complete"] is False


@pytest.mark.asyncio
async def test_arm_then_state_is_counting_down(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Arm 5 minutes from now.
        future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        resp = await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
        assert resp.status_code == 200
        # Re-fetch state.
        resp = await client.get("/api/race-start/state")
    body = resp.json()
    assert body["phase"] == "counting_down"
    assert body["kind"] == "5-4-1-0"


@pytest.mark.asyncio
async def test_arm_unknown_kind_is_400(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/race-start/arm",
            json={"kind": "10-6-5-4-1-0", "t0_utc": T0.isoformat()},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_arm_naive_datetime_is_400(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": "2026-05-01T13:45:00"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sync_from_idle_is_409(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/race-start/sync", json={"expected_signal_offset_s": 0}
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_nudge_shifts_t0(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
        resp = await client.post("/api/race-start/nudge", json={"delta_s": 60})
    body = resp.json()
    assert resp.status_code == 200
    new_t0 = datetime.fromisoformat(body["t0_utc"])
    original = datetime.fromisoformat(future_t0)
    assert (new_t0 - original).total_seconds() == pytest.approx(60.0, abs=0.001)


@pytest.mark.asyncio
async def test_postpone_then_resume(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
        r = await client.post("/api/race-start/postpone")
        assert r.status_code == 200
        assert r.json()["phase"] == "postponed"

        resume_t0 = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        r = await client.post(
            "/api/race-start/resume", json={"new_t0_utc": resume_t0}
        )
        assert r.status_code == 200
        assert r.json()["phase"] == "counting_down"


@pytest.mark.asyncio
async def test_recall_then_restart(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
        r = await client.post("/api/race-start/recall")
        assert r.status_code == 200
        assert r.json()["phase"] == "general_recall"

        new_t0 = (datetime.now(UTC) + timedelta(minutes=8)).isoformat()
        r = await client.post(
            "/api/race-start/restart", json={"new_t0_utc": new_t0}
        )
        assert r.status_code == 200
        # Restart re-arms; tick will advance to counting_down on next read.
        assert r.json()["phase"] in {"armed", "counting_down"}


@pytest.mark.asyncio
async def test_abandon_then_reset(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
        r = await client.post("/api/race-start/abandon")
        assert r.status_code == 200
        assert r.json()["phase"] == "abandoned"

        r = await client.post("/api/race-start/reset")
        assert r.status_code == 200
        assert r.json()["phase"] == "idle"


# ---------------------------------------------------------------------------
# Line pings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_boat_then_pin_completes_line(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 47.6500, "longitude_deg": -122.4000},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["start_line"]["boat_end_lat"] == 47.6500
        assert body["start_line"]["is_complete"] is False

        r = await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.6510, "longitude_deg": -122.4000},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["start_line"]["pin_end_lat"] == 47.6510
        assert body["start_line"]["is_complete"] is True


@pytest.mark.asyncio
async def test_ping_invalid_coords_400(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 999.0, "longitude_deg": 0.0},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_line_metrics_without_pings_returns_null(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/line-metrics",
            json={"boat_lat": 47.65, "boat_lon": -122.40, "sog_kn": 5.0},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None


@pytest.mark.asyncio
async def test_line_metrics_with_complete_line(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 47.65, "longitude_deg": -122.4000},
        )
        await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.65, "longitude_deg": -122.3987},
        )
        r = await client.post(
            "/api/race-start/line-metrics",
            json={
                "boat_lat": 47.6505,
                "boat_lon": -122.40,
                "sog_kn": 5.0,
                "twd_deg": 0.0,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is not None
    assert body["metrics"]["line_length_m"] > 0


# ---------------------------------------------------------------------------
# Auth gating (AUTH_DISABLED=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_unauthenticated_is_401(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/state")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_viewer_can_read_state(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/api/race-start/state")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_viewer_blocked_from_arm(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            r = await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_viewer_blocked_from_ping(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.post(
                "/api/race-start/ping/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_crew_can_arm(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_crew(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            r = await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_viewer_template_shows_readonly_banner(storage: Storage) -> None:
    """Viewer page renders a 'read only' banner; mutation buttons are disabled."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/race-start")
        assert r.status_code == 200
        assert "Viewer mode" in r.text
        assert "disabled" in r.text


# ---------------------------------------------------------------------------
# Persistence — state survives "page reload"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_persists_across_clients(storage: Storage) -> None:
    """Arming via one client and reading via a fresh client returns the same
    state — the singleton row is the source of truth (#644 EARS §E)."""
    app = create_app(storage)
    future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/race-start/arm",
            json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
        )
    # Fresh client.
    app2 = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://test"
    ) as client:
        r = await client.get("/api/race-start/state")
    body = r.json()
    assert body["phase"] in {"armed", "counting_down"}
    assert body["kind"] == "5-4-1-0"
