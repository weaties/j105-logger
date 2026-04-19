"""API tests for anchored thread routes + anchor-picker endpoint (#478)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_T0 = "2024-06-15T12:00:00+00:00"
_T1 = "2024-06-15T12:00:30+00:00"


async def _make_session(client: httpx.AsyncClient) -> int:
    await client.post("/api/event", json={"event_name": "ThreadTest"})
    resp = await client.post("/api/races/start")
    assert resp.status_code == 201
    return resp.json()["id"]


async def _make_session_direct(storage: Storage, idx: int) -> int:
    now = datetime.now(UTC).isoformat()
    assert storage._db is not None
    cur = await storage._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"t-{idx}", "E", idx, "2024-06-15", now),
    )
    await storage._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def _seed_maneuver(storage: Storage, session_id: int, ts: str = _T0) -> int:
    assert storage._db is not None
    cur = await storage._db.execute(
        "INSERT INTO maneuvers (session_id, type, ts, end_ts) VALUES (?, 'tack', ?, ?)",
        (session_id, ts, _T1),
    )
    await storage._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# POST /threads with new anchor shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_thread_with_timestamp_anchor(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"title": "Layline", "anchor": {"kind": "timestamp", "t_start": _T0}},
        )
        assert resp.status_code == 201
        tid = resp.json()["id"]

        resp = await client.get(f"/api/threads/{tid}")
        data = resp.json()
        assert data["title"] == "Layline"
        assert data["anchor"] == {"kind": "timestamp", "t_start": _T0}


@pytest.mark.asyncio
async def test_create_thread_no_anchor(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(f"/api/sessions/{sid}/threads", json={"title": "General"})
        assert resp.status_code == 201
        tid = resp.json()["id"]
        data = (await client.get(f"/api/threads/{tid}")).json()
        assert data["anchor"] is None


@pytest.mark.asyncio
async def test_create_thread_with_maneuver_anchor(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        mid = await _seed_maneuver(storage, sid)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"anchor": {"kind": "maneuver", "entity_id": mid}},
        )
        assert resp.status_code == 201
        tid = resp.json()["id"]
        data = (await client.get(f"/api/threads/{tid}")).json()
        assert data["anchor"] == {"kind": "maneuver", "entity_id": mid}


@pytest.mark.asyncio
async def test_create_thread_rejects_legacy_payload(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"title": "x", "anchor_timestamp": _T0},
        )
        assert resp.status_code == 400
        assert "anchor_timestamp" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_thread_rejects_legacy_mark_reference(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"mark_reference": "weather_mark_1"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_thread_rejects_cross_session_maneuver(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid_a = await _make_session_direct(storage, 1)
        sid_b = await _make_session_direct(storage, 2)
        mid_in_b = await _seed_maneuver(storage, sid_b)
        resp = await client.post(
            f"/api/sessions/{sid_a}/threads",
            json={"anchor": {"kind": "maneuver", "entity_id": mid_in_b}},
        )
        assert resp.status_code == 400
        assert "maneuver" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_thread_rejects_bad_anchor_shape(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"anchor": {"kind": "timestamp"}},  # missing t_start
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_thread_rejects_maneuver_with_t_start(storage: Storage) -> None:
    """Regression: the anchor-picker used to forward the entity's display
    timestamp as an Anchor field, which the validator rightly rejects."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        mid = await _seed_maneuver(storage, sid)
        resp = await client.post(
            f"/api/sessions/{sid}/threads",
            json={"anchor": {"kind": "maneuver", "entity_id": mid, "t_start": _T0}},
        )
        assert resp.status_code == 400
        assert "t_start" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_threads_projects_anchor(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        await client.post(
            f"/api/sessions/{sid}/threads",
            json={"title": "with anchor", "anchor": {"kind": "timestamp", "t_start": _T0}},
        )
        await client.post(f"/api/sessions/{sid}/threads", json={"title": "no anchor"})
        resp = await client.get(f"/api/sessions/{sid}/threads")
        assert resp.status_code == 200
        threads = resp.json()["threads"]
        anchors = [t["anchor"] for t in threads]
        assert {"kind": "timestamp", "t_start": _T0} in anchors
        assert None in anchors


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/anchors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_session_anchors_endpoint(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        await _seed_maneuver(storage, sid)
        await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "bm", "t_start": _T1},
        )
        resp = await client.get(f"/api/sessions/{sid}/anchors")
        assert resp.status_code == 200
        data = resp.json()
        kinds = {a["kind"] for a in data}
        assert {"race", "start", "maneuver", "bookmark"} <= kinds


@pytest.mark.asyncio
async def test_list_session_anchors_empty_for_missing_session(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/anchors")
        assert resp.status_code == 200
        assert resp.json() == []
