"""Tests for session rename + human-readable URL slugs (#449).

Covers the decision table from the structured spec: rename API auth matrix,
slug collision handling, id→slug redirect, retired-slug redirect + expiry,
and 404 fall-through.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helmlog.storage import Storage


_START = datetime(2026, 4, 8, 19, 0, 0, tzinfo=UTC)


async def _create_user(storage: Storage, role: str) -> str:
    email = f"{role}@rename-test.com"
    user_id = await storage.create_user(email, f"Test {role}", role)
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return session_id


@pytest_asyncio.fixture
async def admin_client(storage: Storage) -> AsyncIterator[httpx.AsyncClient]:
    """Admin-authed HTTP client."""
    session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
            follow_redirects=False,
        ) as client:
            yield client


@pytest_asyncio.fixture
async def viewer_client(storage: Storage) -> AsyncIterator[httpx.AsyncClient]:
    """Read-only viewer client — rename must 403."""
    session_id = await _create_user(storage, "viewer")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
            follow_redirects=False,
        ) as client:
            yield client


@pytest_asyncio.fixture
async def anon_client(storage: Storage) -> AsyncIterator[httpx.AsyncClient]:
    """Unauthenticated client — rename must 401."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            yield client


async def _seed_race(storage: Storage, *, name: str, race_num: int = 1) -> int:
    race = await storage.start_race("CYC Spring", _START, "2026-04-08", race_num, name)
    await storage.end_race(race.id, _START + timedelta(hours=1))
    return race.id


# ---------------------------------------------------------------------------
# PATCH /api/sessions/{id} — auth matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_admin_success(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-4")
    resp = await admin_client.patch(
        f"/api/sessions/{race_id}",
        json={"name": "Ballard Cup #1 — finish line confusion"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Ballard Cup #1 — finish line confusion"
    assert body["slug"] == "ballard-cup-1-finish-line-confusion"
    assert body["retired_slug"] == "20260408-cyc-4"
    assert body["url"] == "/session/ballard-cup-1-finish-line-confusion"

    # Audit row written
    audit_rows = await storage.list_audit_log(limit=5)
    actions = [row["action"] for row in audit_rows]
    assert "race.rename" in actions


@pytest.mark.asyncio
async def test_rename_viewer_forbidden(storage: Storage, viewer_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-4")
    resp = await viewer_client.patch(f"/api/sessions/{race_id}", json={"name": "New Name"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_rename_unauthenticated_401(storage: Storage, anon_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-4")
    resp = await anon_client.patch(f"/api/sessions/{race_id}", json={"name": "New Name"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rename_name_collision_409(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    await _seed_race(storage, name="20260408-CYC-1", race_num=1)
    race2_id = await _seed_race(
        storage,
        name="20260408-CYC-2",
        race_num=2,
    )
    resp = await admin_client.patch(
        f"/api/sessions/{race2_id}",
        json={"name": "20260408-CYC-1"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == {"error": "name_taken"}


@pytest.mark.asyncio
async def test_rename_nonexistent_race_404(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    resp = await admin_client.patch("/api/sessions/99999", json={"name": "X"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_empty_body_422(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    resp = await admin_client.patch(f"/api/sessions/{race_id}", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rename_event_and_race_num_regenerates_name(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    resp = await admin_client.patch(
        f"/api/sessions/{race_id}",
        json={"event": "BallardCup", "race_num": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "20260408-BallardCup-3"
    assert body["slug"] == "20260408-ballardcup-3"


# ---------------------------------------------------------------------------
# Slug routing — /session/{slug} and /session/{id} redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_int_id_redirects_to_slug(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    resp = await admin_client.get(f"/session/{race_id}")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/session/20260408-cyc-1"


@pytest.mark.asyncio
async def test_current_slug_renders_page(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    await _seed_race(storage, name="20260408-CYC-1")
    resp = await admin_client.get("/session/20260408-cyc-1")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_retired_slug_redirects_within_window(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    await storage.rename_race(race_id, new_name="New Name")
    resp = await admin_client.get("/session/20260408-cyc-1")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/session/new-name"


@pytest.mark.asyncio
async def test_retired_slug_expired_404(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    await storage.rename_race(race_id, new_name="New Name")
    # Backdate the history row past the 30-day retention window.
    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    db = storage._conn()  # noqa: SLF001
    await db.execute(
        "UPDATE race_slug_history SET retired_at = ? WHERE slug = ?",
        (old_ts, "20260408-cyc-1"),
    )
    await db.commit()
    resp = await admin_client.get("/session/20260408-cyc-1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_slug_404(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.get("/session/never-existed")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_int_id_404(storage: Storage, admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.get("/session/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Video pipeline — rename must not break race_videos links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_does_not_touch_race_videos(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    race_id = await _seed_race(storage, name="20260408-CYC-1")
    await storage.add_race_video(
        race_id=race_id,
        youtube_url="https://youtu.be/abc",
        video_id="abc",
        title="stern cam",
        label="stern",
        sync_utc=_START,
        sync_offset_s=0.0,
    )
    resp = await admin_client.patch(f"/api/sessions/{race_id}", json={"name": "Totally New"})
    assert resp.status_code == 200

    videos = await storage.list_race_videos(race_id)
    assert len(videos) == 1
    assert videos[0]["youtube_url"] == "https://youtu.be/abc"
