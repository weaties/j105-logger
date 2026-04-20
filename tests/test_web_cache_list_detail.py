"""Tests for T1 caching on /api/sessions list + /detail and tag-mutation
invalidation hooks (#608)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.cache import WebCache
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


_START = datetime(2026, 2, 26, 14, 0, 0, tzinfo=UTC)
_END = datetime(2026, 2, 26, 14, 30, 0, tzinfo=UTC)


async def _seed_completed_race(storage: Storage, race_num: int = 1) -> int:
    race = await storage.start_race(
        event="E",
        start_utc=_START + timedelta(minutes=race_num),
        date_str="2026-02-26",
        race_num=race_num,
        name=f"Race {race_num}",
    )
    db = storage._conn()
    for i in range(3):
        ts = (_START + timedelta(minutes=race_num, seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, 37.7, -122.4, race.id),
        )
    await db.commit()
    await storage.end_race(race.id, _END + timedelta(minutes=race_num))
    return race.id


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_invalidate_family_drops_matching_entries(storage: Storage) -> None:
    """`t1_invalidate_family("sessions_list")` must drop every entry in that
    family (the matching used is prefix-based, see cache.t1_invalidate_family)."""
    cache = WebCache(storage)
    cache.t1_put("sessions_list:abc", {"v": 1}, ttl_seconds=60)
    cache.t1_put("sessions_list:def", {"v": 2}, ttl_seconds=60)
    cache.t1_put("other:keep", {"v": 3}, ttl_seconds=60)

    cache.t1_invalidate_family("sessions_list")

    assert cache.t1_get("sessions_list:abc") is None
    assert cache.t1_get("sessions_list:def") is None
    assert cache.t1_get("other:keep") == {"v": 3}


@pytest.mark.asyncio
async def test_race_invalidate_also_drops_sessions_list(storage: Storage) -> None:
    """race-mutation hook must flush the sessions_list family, not just per-race."""
    cache = WebCache(storage)
    cache.t1_put("sessions_list:abc", {"v": 1}, ttl_seconds=60)
    cache.t1_put_for_race("session_detail", race_id=42, value={"v": 2}, ttl_seconds=60)

    await cache.invalidate(42)

    assert cache.t1_get("sessions_list:abc") is None
    assert cache.t1_get_for_race("session_detail", race_id=42) is None


# ---------------------------------------------------------------------------
# Route — session list T1 caching + invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_list_cache_hit_returns_same_body(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    await _seed_completed_race(storage, 1)
    await _seed_completed_race(storage, 2)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/api/sessions")
        hit = await client.get("/api/sessions")
    assert first.status_code == 200
    assert hit.status_code == 200
    assert first.json() == hit.json()


@pytest.mark.asyncio
async def test_sessions_list_writes_to_t1(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/sessions")

    cache: WebCache = app.state.web_cache
    # There should be at least one sessions_list entry after the request.
    assert any(k.startswith("sessions_list:") for k in cache._t1)


@pytest.mark.asyncio
async def test_sessions_list_different_filters_different_keys(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/sessions?type=race")
        await client.get("/api/sessions?type=practice")

    cache: WebCache = app.state.web_cache
    list_keys = [k for k in cache._t1 if k.startswith("sessions_list:")]
    assert len(list_keys) == 2  # distinct filter combos → distinct keys


@pytest.mark.asyncio
async def test_race_mutation_invalidates_sessions_list(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/sessions")
        cache: WebCache = app.state.web_cache
        assert any(k.startswith("sessions_list:") for k in cache._t1)

        await storage.rename_race(race_id, new_name="Renamed")

        assert not any(k.startswith("sessions_list:") for k in cache._t1)


# ---------------------------------------------------------------------------
# Route — session detail T1 caching + invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_detail_cache_hit_returns_same_body(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get(f"/api/sessions/{race_id}/detail")
        hit = await client.get(f"/api/sessions/{race_id}/detail")
    assert first.json() == hit.json()


@pytest.mark.asyncio
async def test_session_detail_404_bypasses_cache(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/detail")
    assert resp.status_code == 404

    cache: WebCache = app.state.web_cache
    assert not any("session_detail" in k for k in cache._t1)


@pytest.mark.asyncio
async def test_session_detail_invalidates_on_race_mutation(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(f"/api/sessions/{race_id}/detail")
        name_before = r1.json()["name"]

        await storage.rename_race(race_id, new_name="After Rename")

        r2 = await client.get(f"/api/sessions/{race_id}/detail")
        name_after = r2.json()["name"]

    assert name_before == "Race 1"
    assert name_after == "After Rename"


# ---------------------------------------------------------------------------
# Tag-mutation invalidation hooks on storage (#608 core)
# ---------------------------------------------------------------------------


class _Recorder:
    """Mock cache that records invalidate + family-drop calls."""

    def __init__(self) -> None:
        self.invalidated_races: list[int] = []
        self.families_dropped: list[str] = []

    async def invalidate(self, race_id: int) -> None:
        self.invalidated_races.append(race_id)

    def t1_invalidate_family(self, family: str) -> None:
        self.families_dropped.append(family)


@pytest.mark.asyncio
async def test_attach_tag_invalidates_sessions_list(storage: Storage) -> None:
    race_id = await _seed_completed_race(storage, 1)
    # Create a tag
    db = storage._conn()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("foo", datetime.now(UTC).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("foo",))
    tag_id = int((await cur.fetchone())["id"])

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    await storage.attach_tag("session", race_id, tag_id, user_id=None)

    assert "sessions_list" in recorder.families_dropped


@pytest.mark.asyncio
async def test_attach_tag_noop_does_not_invalidate(storage: Storage) -> None:
    """Re-attaching an existing tag is idempotent — no invalidation needed."""
    race_id = await _seed_completed_race(storage, 1)
    db = storage._conn()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("foo", datetime.now(UTC).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("foo",))
    tag_id = int((await cur.fetchone())["id"])

    await storage.attach_tag("session", race_id, tag_id, user_id=None)

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    # Second attach is a no-op
    await storage.attach_tag("session", race_id, tag_id, user_id=None)

    assert recorder.families_dropped == []


@pytest.mark.asyncio
async def test_detach_tag_invalidates_sessions_list(storage: Storage) -> None:
    race_id = await _seed_completed_race(storage, 1)
    db = storage._conn()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("foo", datetime.now(UTC).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("foo",))
    tag_id = int((await cur.fetchone())["id"])
    await storage.attach_tag("session", race_id, tag_id, user_id=None)

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    removed = await storage.detach_tag("session", race_id, tag_id)

    assert removed is True
    assert "sessions_list" in recorder.families_dropped


@pytest.mark.asyncio
async def test_update_tag_invalidates_sessions_list(storage: Storage) -> None:
    db = storage._conn()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("foo", datetime.now(UTC).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("foo",))
    tag_id = int((await cur.fetchone())["id"])

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    changed = await storage.update_tag(tag_id, name="bar")

    assert changed is True
    assert "sessions_list" in recorder.families_dropped


@pytest.mark.asyncio
async def test_delete_tag_invalidates_sessions_list(storage: Storage) -> None:
    db = storage._conn()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("foo", datetime.now(UTC).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("foo",))
    tag_id = int((await cur.fetchone())["id"])

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    removed = await storage.delete_tag(tag_id)

    assert removed is True
    assert "sessions_list" in recorder.families_dropped


@pytest.mark.asyncio
async def test_merge_tags_invalidates_sessions_list(storage: Storage) -> None:
    db = storage._conn()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("src", now),
    )
    await db.execute(
        "INSERT INTO tags (name, created_at, usage_count) VALUES (?, ?, 0)",
        ("tgt", now),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("src",))
    src_id = int((await cur.fetchone())["id"])
    cur = await db.execute("SELECT id FROM tags WHERE name = ?", ("tgt",))
    tgt_id = int((await cur.fetchone())["id"])

    recorder = _Recorder()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    await storage.merge_tags(src_id, tgt_id)

    assert "sessions_list" in recorder.families_dropped
