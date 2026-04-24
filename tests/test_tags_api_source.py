"""API tests for tag source / confirmation flow (#650).

Covers:
- GET list endpoint hides unconfirmed auto-tags by default
- GET ?include_unconfirmed=true surfaces them
- POST confirm endpoint sets confirmed_at / confirmed_by
- POST attach always writes source='manual' even if the body tries to override
- Response bodies include source and confirmed_at so clients can render
  a "pending review" marker
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


@pytest.mark.asyncio
async def test_list_endpoint_hides_unconfirmed_auto_by_default(storage: Storage) -> None:
    sid = await _seed_session(storage)
    t_manual = await storage.create_tag("api-manual")
    t_auto = await storage.create_tag("api-auto")
    await storage.attach_tag("session", sid, t_manual, user_id=None)
    await storage.attach_tag("session", sid, t_auto, user_id=None, source="auto:transcript")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/entities/session/{sid}/tags")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert "api-manual" in names
        assert "api-auto" not in names


@pytest.mark.asyncio
async def test_list_endpoint_includes_unconfirmed_via_flag(storage: Storage) -> None:
    sid = await _seed_session(storage)
    t_auto = await storage.create_tag("api-auto2")
    await storage.attach_tag("session", sid, t_auto, user_id=None, source="auto:detector")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/entities/session/{sid}/tags?include_unconfirmed=true")
        assert resp.status_code == 200
        tags = resp.json()
        auto = next(t for t in tags if t["name"] == "api-auto2")
        assert auto["source"] == "auto:detector"
        assert auto["confirmed_at"] is None


@pytest.mark.asyncio
async def test_post_attach_is_always_manual(storage: Storage) -> None:
    """A client cannot forge an auto-source via the HTTP body. Only
    backend code paths (auto-taggers) can write auto sources."""
    sid = await _seed_session(storage)
    tid = await storage.create_tag("forge-attempt")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/entities/session/{sid}/tags",
            json={"tag_id": tid, "source": "auto:transcript"},
        )
        assert resp.status_code == 201

    assert storage._db is not None
    cur = await storage._db.execute(
        "SELECT source FROM entity_tags WHERE tag_id = ? AND entity_id = ?",
        (tid, sid),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["source"] == "manual", "HTTP body source field must be ignored"


@pytest.mark.asyncio
async def test_confirm_endpoint_sets_confirmation(storage: Storage) -> None:
    sid = await _seed_session(storage)
    tid = await storage.create_tag("to-confirm-api")
    await storage.attach_tag("session", sid, tid, user_id=None, source="auto:transcript")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/entities/session/{sid}/tags/{tid}/confirm")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["confirmed"] is True

        # After confirm, the tag appears in the default list.
        resp = await client.get(f"/api/entities/session/{sid}/tags")
        names = {t["name"] for t in resp.json()}
        assert "to-confirm-api" in names


@pytest.mark.asyncio
async def test_confirm_endpoint_404_when_not_attached(storage: Storage) -> None:
    sid = await _seed_session(storage)
    tid = await storage.create_tag("never-attached")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/entities/session/{sid}/tags/{tid}/confirm")
        assert resp.status_code == 404
