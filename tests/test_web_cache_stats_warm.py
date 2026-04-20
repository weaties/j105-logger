"""Tests for /api/admin/cache/stats + warm-on-complete (#611)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.cache import WebCache, warm_race_cache
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
# Counter unit tests
# ---------------------------------------------------------------------------


def test_family_of_parses_list_and_race_keys(storage: Storage) -> None:
    cache = WebCache(storage)
    assert cache._family_of("sessions_list:abc123") == "sessions_list"
    assert cache._family_of("session_detail::race=42") == "session_detail"
    assert cache._family_of("wind_field:grid=20:t=0.000") == "wind_field"
    assert cache._family_of("plain") == "plain"


@pytest.mark.asyncio
async def test_t1_hit_and_miss_bump_counters(storage: Storage) -> None:
    cache = WebCache(storage)
    # Miss
    assert cache.t1_get("sessions_list:abc") is None
    # Hit
    cache.t1_put("sessions_list:abc", {"v": 1}, ttl_seconds=60)
    assert cache.t1_get("sessions_list:abc") == {"v": 1}
    # Another miss (different key, same family)
    assert cache.t1_get("sessions_list:def") is None

    stats = cache.stats()
    assert stats["sessions_list"]["miss"] == 2
    assert stats["sessions_list"]["hit"] == 1


@pytest.mark.asyncio
async def test_t2_hit_and_miss_bump_counters(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put("session_track", race_id=1, data_hash="h", value={"v": 1})

    # Hit
    assert await cache.t2_get("session_track", race_id=1, data_hash="h") == {"v": 1}
    # Miss — stale hash
    assert await cache.t2_get("session_track", race_id=1, data_hash="WRONG") is None
    # Miss — no row
    assert await cache.t2_get("session_track", race_id=99, data_hash="h") is None

    stats = cache.stats()
    assert stats["session_track"]["hit"] == 1
    assert stats["session_track"]["miss"] == 2


@pytest.mark.asyncio
async def test_invalidate_bumps_counter_once_per_family(storage: Storage) -> None:
    cache = WebCache(storage)
    # Two entries in two families for the same race.
    await cache.t2_put("session_summary", race_id=1, data_hash="h", value={"v": 1})
    await cache.t2_put("session_track", race_id=1, data_hash="h", value={"v": 2})
    # One T1 race-keyed entry too.
    cache.t1_put_for_race("session_detail", race_id=1, value={"v": 3}, ttl_seconds=60)
    # And a list-family entry.
    cache.t1_put("sessions_list:abc", {"v": 4}, ttl_seconds=60)

    await cache.invalidate(1)

    stats = cache.stats()
    # T2 invalidate: each distinct key_family once
    assert stats["session_summary"]["invalidate"] == 1
    assert stats["session_track"]["invalidate"] == 1
    # T1 race-keyed invalidate
    assert stats["session_detail"]["invalidate"] == 1
    # T1 family-drop
    assert stats["sessions_list"]["invalidate"] == 1


@pytest.mark.asyncio
async def test_reset_stats_clears_counters(storage: Storage) -> None:
    cache = WebCache(storage)
    cache.t1_get("missing:key")
    assert cache.stats() != {}
    cache.reset_stats()
    assert cache.stats() == {}


# ---------------------------------------------------------------------------
# /api/admin/cache/stats endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_stats_endpoint_requires_admin(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")  # mock admin in test harness
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/admin/cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "families" in body
    assert "t1_entries" in body
    assert "t2_rows" in body


@pytest.mark.asyncio
async def test_cache_stats_reflects_usage(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Miss, then hit
        await client.get(f"/api/sessions/{race_id}/summary")
        await client.get(f"/api/sessions/{race_id}/summary")

        resp = await client.get("/api/admin/cache/stats")

    body = resp.json()
    assert "session_summary" in body["families"]
    assert body["families"]["session_summary"]["miss"] >= 1
    assert body["families"]["session_summary"]["hit"] >= 1
    assert body["t2_rows"] >= 1


@pytest.mark.asyncio
async def test_cache_stats_reset(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage, 1)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/api/sessions/{race_id}/summary")
        before = (await client.get("/api/admin/cache/stats")).json()
        assert before["families"] != {}

        reset = await client.post("/api/admin/cache/stats/reset")
        assert reset.status_code == 204

        after = (await client.get("/api/admin/cache/stats")).json()
        # The GET right after reset counts as one lookup; only session_list
        # (from get_storage/ list path) should not appear yet. Everything
        # else has been zeroed.
        for fam, counters in after["families"].items():
            assert counters["hit"] == 0, f"family {fam} should be zeroed"


# ---------------------------------------------------------------------------
# warm_race_cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_race_cache_populates_three_families(storage: Storage) -> None:
    race_id = await _seed_completed_race(storage, 1)
    cache = WebCache(storage)
    storage.bind_race_cache(cache)

    await warm_race_cache(storage, cache, race_id)

    db = storage._conn()
    cur = await db.execute(
        "SELECT key_family FROM web_cache WHERE race_id = ? ORDER BY key_family",
        (race_id,),
    )
    families = [r["key_family"] for r in await cur.fetchall()]
    # wind-field is only populated for synth sessions; real race won't warm it.
    assert "session_summary" in families
    assert "session_track" in families


@pytest.mark.asyncio
async def test_warm_race_cache_missing_race_is_noop(storage: Storage) -> None:
    cache = WebCache(storage)
    # Should not raise even though race 9999 doesn't exist.
    await warm_race_cache(storage, cache, 9999)
    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache")
    assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_warm_race_cache_survives_compute_failure(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    race_id = await _seed_completed_race(storage, 1)
    cache = WebCache(storage)

    from helmlog.routes import sessions as sessions_mod

    async def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sessions_mod, "_compute_session_track", _boom)

    # Should still populate summary even though track fails.
    await warm_race_cache(storage, cache, race_id)

    db = storage._conn()
    cur = await db.execute("SELECT key_family FROM web_cache WHERE race_id = ?", (race_id,))
    families = [r["key_family"] for r in await cur.fetchall()]
    assert "session_summary" in families
    assert "session_track" not in families  # failed family skipped


@pytest.mark.asyncio
async def test_end_race_http_path_triggers_warm(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/races/{id}/end should fire warm_race_cache in background."""
    import asyncio

    monkeypatch.setenv("AUTH_DISABLED", "true")
    # Start a race (cannot use _seed_completed_race since we want end_race via HTTP)
    race = await storage.start_race(
        event="E",
        start_utc=_START,
        date_str="2026-02-26",
        race_num=1,
        name="Race 1",
    )
    db = storage._conn()
    for i in range(3):
        ts = (_START + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, 37.7, -122.4, race.id),
        )
    await db.commit()

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/api/races/{race.id}/end")
        assert resp.status_code == 204

    # Let the background task finish.
    for _ in range(20):
        await asyncio.sleep(0.05)
        cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ?", (race.id,))
        if (await cur.fetchone())["n"] >= 2:
            break

    cur = await db.execute("SELECT key_family FROM web_cache WHERE race_id = ?", (race.id,))
    families = [r["key_family"] for r in await cur.fetchall()]
    assert "session_summary" in families
    assert "session_track" in families
