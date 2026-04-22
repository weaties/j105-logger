"""Storage + API tests for bookmark counterparty (#651).

Covers:
- create_bookmark persists counterparty
- update_bookmark sets / clears / leaves counterparty alone per sentinel
- list_bookmark_counterparties returns distinct non-null values sorted
- POST /api/sessions/{id}/bookmarks accepts counterparty
- PATCH /api/bookmarks/{id} — null clears, absent leaves alone
- GET /api/bookmarks/counterparties typeahead endpoint
- Serialized response includes counterparty
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _seed_session(storage: Storage, idx: int = 1) -> int:
    now = datetime.now(UTC).isoformat()
    assert storage._db is not None
    cur = await storage._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"s{idx}", "E", idx, "2024-06-15", now),
    )
    await storage._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_bookmark_persists_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="close crossing",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["counterparty"] == "Absolutely"


@pytest.mark.asyncio
async def test_create_bookmark_without_counterparty_defaults_null(storage: Storage) -> None:
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="no counterparty",
        note=None,
        t_start="2024-06-15T12:00:30",
    )
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["counterparty"] is None


@pytest.mark.asyncio
async def test_update_bookmark_sets_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="bm",
        note=None,
        t_start="2024-06-15T12:00:30",
    )
    await storage.update_bookmark(bm_id, counterparty="Zephyr")
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["counterparty"] == "Zephyr"


@pytest.mark.asyncio
async def test_update_bookmark_clears_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="bm",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    await storage.update_bookmark(bm_id, clear_counterparty=True)
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["counterparty"] is None


@pytest.mark.asyncio
async def test_update_bookmark_absent_counterparty_leaves_alone(storage: Storage) -> None:
    """Name-only update must not clobber the counterparty."""
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="bm",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    await storage.update_bookmark(bm_id, name="renamed")
    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["name"] == "renamed"
    assert bm["counterparty"] == "Absolutely"


@pytest.mark.asyncio
async def test_list_bookmark_counterparties_distinct_sorted(storage: Storage) -> None:
    sid = await _seed_session(storage)
    for cp, name in [
        ("Absolutely", "a"),
        ("Zephyr", "b"),
        ("Absolutely", "c"),
        (None, "d"),
        ("Mistral", "e"),
    ]:
        await storage.create_bookmark(
            session_id=sid,
            user_id=None,
            name=name,
            note=None,
            t_start="2024-06-15T12:00:30",
            counterparty=cp,
        )
    values = await storage.list_bookmark_counterparties()
    assert values == ["Absolutely", "Mistral", "Zephyr"]


@pytest.mark.asyncio
async def test_list_bookmarks_for_session_includes_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="with",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    rows = await storage.list_bookmarks_for_session(sid)
    assert len(rows) == 1
    assert rows[0]["counterparty"] == "Absolutely"


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_bookmark_accepts_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={
                "name": "close crossing",
                "t_start": "2024-06-15T12:00:30",
                "counterparty": "Absolutely",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["counterparty"] == "Absolutely"


@pytest.mark.asyncio
async def test_patch_bookmark_null_clears_counterparty(storage: Storage) -> None:
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="bm",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch(f"/api/bookmarks/{bm_id}", json={"counterparty": None})
        assert resp.status_code == 200
        assert resp.json()["counterparty"] is None


@pytest.mark.asyncio
async def test_patch_bookmark_absent_counterparty_leaves_alone(storage: Storage) -> None:
    """PATCH without counterparty key must not clobber — many existing
    callers send only name / note."""
    sid = await _seed_session(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid,
        user_id=None,
        name="bm",
        note=None,
        t_start="2024-06-15T12:00:30",
        counterparty="Absolutely",
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch(f"/api/bookmarks/{bm_id}", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["counterparty"] == "Absolutely"
        assert resp.json()["name"] == "renamed"


@pytest.mark.asyncio
async def test_get_counterparties_typeahead_endpoint(storage: Storage) -> None:
    sid = await _seed_session(storage)
    for cp in ("Absolutely", "Zephyr", "Absolutely"):
        await storage.create_bookmark(
            session_id=sid,
            user_id=None,
            name="bm",
            note=None,
            t_start="2024-06-15T12:00:30",
            counterparty=cp,
        )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/bookmarks/counterparties")
        assert resp.status_code == 200
        assert resp.json() == ["Absolutely", "Zephyr"]


@pytest.mark.asyncio
async def test_post_bookmark_blank_counterparty_stored_as_null(storage: Storage) -> None:
    """Blank strings from the UI typeahead shouldn't clutter DISTINCT queries."""
    sid = await _seed_session(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={
                "name": "bm",
                "t_start": "2024-06-15T12:00:30",
                "counterparty": "   ",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["counterparty"] is None
