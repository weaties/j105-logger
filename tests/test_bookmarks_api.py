"""Tests for the bookmark HTTP API (decision table 2 from the /spec on #477).

Permission matrix under test:

| Actor          | Author     | Action          | Expected |
|----------------|------------|-----------------|----------|
| authed user    | self       | create          | 201      |
| authed user    | self       | list            | 200      |
| authed user    | self       | rename/delete   | 200/204  |
| authed user    | other user | rename/delete   | 403      |
| admin          | any        | rename/delete   | 200/204  |
| any            | any        | missing session | 404      |
| any            | any        | missing bm_id   | 404      |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

import helmlog.auth as auth_module
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_T0 = "2024-06-15T12:00:00+00:00"
_T1 = "2024-06-15T12:00:30+00:00"


async def _make_session(client: httpx.AsyncClient) -> int:
    await client.post("/api/event", json={"event_name": "BookmarkTest"})
    resp = await client.post("/api/races/start")
    assert resp.status_code == 201
    return resp.json()["id"]


_seed_counter = 0


async def _seed_session_direct(storage: Storage) -> int:
    """Create a session via storage so permission tests can run as viewers."""
    global _seed_counter
    _seed_counter += 1
    assert storage._db is not None
    cur = await storage._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (
            f"perm-test-{_seed_counter}",
            "PermTest",
            _seed_counter,
            "2024-06-15",
            _T0,
        ),
    )
    await storage._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_create_bookmark_201_returns_payload(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "Mark 1 rounding", "note": "tight layline", "t_start": _T0},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Mark 1 rounding"
        assert body["note"] == "tight layline"
        assert body["t_start"] == _T0
        assert body["session_id"] == sid


@pytest.mark.asyncio
async def test_create_bookmark_on_missing_session_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/9999/bookmarks",
            json={"name": "nope", "t_start": _T0},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_bookmark_rejects_blank_name(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "   ", "t_start": _T0},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_bookmark_rejects_missing_t_start(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "x"},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_bookmarks_returns_in_time_order(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "late", "t_start": _T1},
        )
        await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "early", "t_start": _T0},
        )
        resp = await client.get(f"/api/sessions/{sid}/bookmarks")
        assert resp.status_code == 200
        names = [b["name"] for b in resp.json()["bookmarks"]]
        assert names == ["early", "late"]


@pytest.mark.asyncio
async def test_list_bookmarks_on_missing_session_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/bookmarks")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_bookmark_as_admin_mock_ok(storage: Storage) -> None:
    """Default test auth is mock admin — edits always allowed."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "old", "note": "orig", "t_start": _T0},
        )
        bid = resp.json()["id"]

        resp = await client.patch(
            f"/api/bookmarks/{bid}",
            json={"name": "new", "note": "updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new"
        assert resp.json()["note"] == "updated"


@pytest.mark.asyncio
async def test_update_bookmark_note_to_null(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "n", "note": "has note", "t_start": _T0},
        )
        bid = resp.json()["id"]

        resp = await client.patch(f"/api/bookmarks/{bid}", json={"note": None})
        assert resp.status_code == 200
        assert resp.json()["note"] is None


@pytest.mark.asyncio
async def test_update_missing_bookmark_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/bookmarks/9999", json={"name": "x"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_bookmark_as_admin_mock(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _make_session(client)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "n", "t_start": _T0},
        )
        bid = resp.json()["id"]

        resp = await client.delete(f"/api/bookmarks/{bid}")
        assert resp.status_code == 204

        resp = await client.get(f"/api/sessions/{sid}/bookmarks")
        assert resp.json()["bookmarks"] == []


@pytest.mark.asyncio
async def test_delete_missing_bookmark_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/bookmarks/9999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Permission tests — patch the mock auth user to simulate different actors
# ---------------------------------------------------------------------------


def _as_viewer(user_id: int) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": f"u{user_id}@example.com",
        "name": f"User {user_id}",
        "role": "viewer",
        "is_developer": 0,
        "created_at": "2024-01-01T00:00:00+00:00",
        "last_seen": None,
        "is_active": 1,
    }


async def _seed_user(storage: Storage, user_id: int) -> None:
    """Insert a real users row so FK constraints are satisfied."""
    assert storage._db is not None
    await storage._db.execute(
        "INSERT OR IGNORE INTO users (id, email, role, created_at) "
        "VALUES (?, ?, 'viewer', '2024-01-01T00:00:00+00:00')",
        (user_id, f"u{user_id}@example.com"),
    )
    await storage._db.commit()


@pytest.mark.asyncio
async def test_non_author_viewer_cannot_edit(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-admin who didn't create the bookmark gets 403 on PATCH."""
    await _seed_user(storage, 1)
    await _seed_user(storage, 2)
    # Step 1: create bookmark as user 1 (patch before request)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(1))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _seed_session_direct(storage)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "mine", "t_start": _T0},
        )
        bid = resp.json()["id"]

        # Step 2: switch to user 2, try to edit
        monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(2))
        resp = await client.patch(f"/api/bookmarks/{bid}", json={"name": "hijacked"})
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_author_viewer_cannot_delete(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_user(storage, 1)
    await _seed_user(storage, 2)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(1))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _seed_session_direct(storage)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "mine", "t_start": _T0},
        )
        bid = resp.json()["id"]

        monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(2))
        resp = await client.delete(f"/api/bookmarks/{bid}")
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_author_viewer_can_edit_and_delete(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_user(storage, 7)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(7))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _seed_session_direct(storage)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "mine", "t_start": _T0},
        )
        bid = resp.json()["id"]

        resp = await client.patch(f"/api/bookmarks/{bid}", json={"name": "renamed"})
        assert resp.status_code == 200

        resp = await client.delete(f"/api/bookmarks/{bid}")
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_admin_can_edit_other_users_bookmark(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin can moderate — edit/delete another author's bookmark."""
    await _seed_user(storage, 1)
    await _seed_user(storage, 99)
    monkeypatch.setattr(auth_module, "_MOCK_ADMIN", _as_viewer(1))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _seed_session_direct(storage)
        resp = await client.post(
            f"/api/sessions/{sid}/bookmarks",
            json={"name": "owned by 1", "t_start": _T0},
        )
        bid = resp.json()["id"]

        admin_user = _as_viewer(99)
        admin_user["role"] = "admin"
        monkeypatch.setattr(auth_module, "_MOCK_ADMIN", admin_user)

        resp = await client.patch(f"/api/bookmarks/{bid}", json={"name": "moderated"})
        assert resp.status_code == 200
        resp = await client.delete(f"/api/bookmarks/{bid}")
        assert resp.status_code == 204
