"""API tests for the tag routes (#587)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import helmlog.auth as auth_module
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _make_session(client: httpx.AsyncClient) -> int:
    await client.post("/api/event", json={"event_name": "TagTest"})
    resp = await client.post("/api/races/start")
    assert resp.status_code == 201
    return resp.json()["id"]


async def _seed_session_direct(storage: Storage, idx: int = 1) -> int:
    now = datetime.now(UTC).isoformat()
    assert storage._db is not None
    cur = await storage._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"tag-{idx}", "E", idx, "2024-06-15", now),
    )
    await storage._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _viewer(user_id: int = 1) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": f"u{user_id}@e.com",
        "name": f"u{user_id}",
        "role": "viewer",
        "is_developer": 0,
        "created_at": "2024-01-01T00:00:00+00:00",
        "last_seen": None,
        "is_active": 1,
    }


async def _seed_user(storage: Storage, user_id: int) -> None:
    assert storage._db is not None
    await storage._db.execute(
        "INSERT OR IGNORE INTO users (id, email, role, created_at) "
        "VALUES (?, ?, 'viewer', '2024-01-01T00:00:00+00:00')",
        (user_id, f"u{user_id}@e.com"),
    )
    await storage._db.commit()


# ---------------------------------------------------------------------------
# /api/tags CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_tag(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/tags", json={"name": "weather"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "weather"

        resp = await client.get("/api/tags")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert "weather" in names


@pytest.mark.asyncio
async def test_create_tag_rejects_blank_name(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/tags", json={"name": "   "})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_tag_rejects_duplicate(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/tags", json={"name": "dupe"})
        resp = await client.post("/api/tags", json={"name": "dupe"})
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_tags_by_usage(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        hot = (await client.post("/api/tags", json={"name": "hot"})).json()["id"]
        cold = (await client.post("/api/tags", json={"name": "cold"})).json()["id"]
        warm = (await client.post("/api/tags", json={"name": "warm"})).json()["id"]
        await client.post(f"/api/entities/session/{sid}/tags", json={"tag_id": hot})
        await client.post(f"/api/entities/session/{sid}/tags", json={"tag_id": warm})
        sid2 = await _seed_session_direct(storage, 2)
        await client.post(f"/api/entities/session/{sid2}/tags", json={"tag_id": hot})

        resp = await client.get("/api/tags?order_by=usage")
        names = [t["name"] for t in resp.json()]
        assert names[0] == "hot"
        # warm (1) ranks above cold (0)
        assert names.index("warm") < names.index("cold")
        # cold has zero usage
        cold_entry = next(t for t in resp.json() if t["id"] == cold)
        assert cold_entry["usage_count"] == 0


@pytest.mark.asyncio
async def test_list_tags_invalid_order_by(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/tags?order_by=bogus")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_tag_requires_admin(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_user(storage, 1)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _viewer(1))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/tags", json={"name": "renameme"})
        tag_id = resp.json()["id"]

        resp = await client.patch(f"/api/tags/{tag_id}", json={"name": "renamed"})
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_tag_admin_ok(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/tags", json={"name": "orig"})
        tag_id = resp.json()["id"]
        resp = await client.patch(f"/api/tags/{tag_id}", json={"name": "updated"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_tag_requires_admin(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_user(storage, 1)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _viewer(1))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/tags", json={"name": "doomed"})
        tag_id = resp.json()["id"]
        resp = await client.delete(f"/api/tags/{tag_id}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_tags_admin_ok(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        src = (await client.post("/api/tags", json={"name": "src"})).json()["id"]
        tgt = (await client.post("/api/tags", json={"name": "tgt"})).json()["id"]
        await client.post(f"/api/entities/session/{sid}/tags", json={"tag_id": src})

        resp = await client.post(f"/api/tags/{src}/merge-into/{tgt}")
        assert resp.status_code == 200

        entity_tags = await client.get(f"/api/entities/session/{sid}/tags")
        ids = [t["id"] for t in entity_tags.json()]
        assert tgt in ids
        assert src not in ids


@pytest.mark.asyncio
async def test_merge_tags_rejects_self(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        tid = (await client.post("/api/tags", json={"name": "same"})).json()["id"]
        resp = await client.post(f"/api/tags/{tid}/merge-into/{tid}")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Polymorphic entity attach/detach/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_by_tag_id(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        tid = (await client.post("/api/tags", json={"name": "x"})).json()["id"]
        resp = await client.post(f"/api/entities/session/{sid}/tags", json={"tag_id": tid})
        assert resp.status_code == 201

        resp = await client.get(f"/api/entities/session/{sid}/tags")
        assert resp.status_code == 200
        assert [t["name"] for t in resp.json()] == ["x"]


@pytest.mark.asyncio
async def test_attach_by_name_inline_creates(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(f"/api/entities/session/{sid}/tags", json={"name": "inline"})
        assert resp.status_code == 201
        # tag now exists
        all_tags = await client.get("/api/tags")
        assert "inline" in [t["name"] for t in all_tags.json()]


@pytest.mark.asyncio
async def test_detach(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        tid = (await client.post("/api/tags", json={"name": "x"})).json()["id"]
        await client.post(f"/api/entities/session/{sid}/tags", json={"tag_id": tid})
        resp = await client.delete(f"/api/entities/session/{sid}/tags/{tid}")
        assert resp.status_code == 204
        resp = await client.get(f"/api/entities/session/{sid}/tags")
        assert resp.json() == []


@pytest.mark.asyncio
async def test_attach_rejects_unknown_entity_type(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/entities/bogus/1/tags", json={"name": "x"})
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_attach_requires_payload(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(f"/api/entities/session/{sid}/tags", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Back-compat per-entity routes still work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bookmark_list_filters_by_tags_and(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        b1 = (
            await client.post(
                f"/api/sessions/{sid}/bookmarks",
                json={"name": "b1", "t_start": "2024-06-15T12:00:00+00:00"},
            )
        ).json()["id"]
        b2 = (
            await client.post(
                f"/api/sessions/{sid}/bookmarks",
                json={"name": "b2", "t_start": "2024-06-15T12:01:00+00:00"},
            )
        ).json()["id"]
        t_red = (await client.post("/api/tags", json={"name": "red"})).json()["id"]
        t_hot = (await client.post("/api/tags", json={"name": "hot"})).json()["id"]
        await client.post(f"/api/entities/bookmark/{b1}/tags", json={"tag_id": t_red})
        await client.post(f"/api/entities/bookmark/{b1}/tags", json={"tag_id": t_hot})
        await client.post(f"/api/entities/bookmark/{b2}/tags", json={"tag_id": t_red})

        # AND: only b1 has both
        resp = await client.get(f"/api/sessions/{sid}/bookmarks?tags={t_red},{t_hot}")
        ids = [b["id"] for b in resp.json()["bookmarks"]]
        assert ids == [b1]

        # OR: both b1 and b2 carry red
        resp = await client.get(f"/api/sessions/{sid}/bookmarks?tags={t_red},{t_hot}&tag_mode=or")
        ids = sorted(b["id"] for b in resp.json()["bookmarks"])
        assert ids == sorted([b1, b2])


@pytest.mark.asyncio
async def test_thread_list_filters_by_tags(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        t1 = (await client.post(f"/api/sessions/{sid}/threads", json={"title": "t1"})).json()["id"]
        t2 = (await client.post(f"/api/sessions/{sid}/threads", json={"title": "t2"})).json()["id"]
        tag = (await client.post("/api/tags", json={"name": "q"})).json()["id"]
        await client.post(f"/api/entities/thread/{t1}/tags", json={"tag_id": tag})

        resp = await client.get(f"/api/sessions/{sid}/threads?tags={tag}")
        ids = [t["id"] for t in resp.json()["threads"]]
        assert ids == [t1]
        assert t2 not in ids


@pytest.mark.asyncio
async def test_filter_accepts_empty_tags_param(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.get(f"/api/sessions/{sid}/bookmarks?tags=")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_filter_invalid_tags_returns_400(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.get(f"/api/sessions/{sid}/bookmarks?tags=abc")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_legacy_session_tag_routes(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(f"/api/sessions/{sid}/tags", json={"tag_name": "legacy-route"})
        assert resp.status_code == 201
        tag_id = resp.json()["tag_id"]

        resp = await client.get(f"/api/sessions/{sid}/tags")
        assert any(t["id"] == tag_id for t in resp.json())

        resp = await client.delete(f"/api/sessions/{sid}/tags/{tag_id}")
        assert resp.status_code == 204
