"""Track endpoint includes prestart positions before start_utc.

Surfaced from real-data testing on 2026-04-30: race-start logged the
helm's prestart maneuvers in the positions table, but the session map
only drew the post-gun track because the track query was bounded at
start_utc. Now we extend the window backwards and pull in any
unscoped positions in that window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


_GUN = datetime(2026, 4, 30, 1, 25, 0, tzinfo=UTC)
_END = _GUN + timedelta(minutes=30)


async def _seed_race_with_prestart(storage: Storage) -> int:
    """Race with pre-gun (race_id IS NULL) and post-gun (race_id=N) pings."""
    race = await storage.start_race(
        event="CYC",
        start_utc=_GUN,
        date_str="2026-04-30",
        race_num=1,
        name="Race 1",
    )
    db = storage._conn()
    # Prestart: 5 minutes before the gun, 6 fixes, race_id IS NULL.
    for i in range(6):
        ts = (_GUN - timedelta(minutes=5) + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 0, 47.65 + i * 0.0001, -122.40 + i * 0.0001),
        )
    # In-race: 5 fixes after the gun, tagged with race_id.
    for i in range(5):
        ts = (_GUN + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, 47.66 + i * 0.0001, -122.41 + i * 0.0001, race.id),
        )
    await db.commit()
    await storage.end_race(race.id, _END)
    return race.id


@pytest.mark.asyncio
async def test_track_includes_prestart_positions(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The track contains both prestart (unscoped) and in-race fixes."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_race_with_prestart(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{race_id}/track")
    assert r.status_code == 200
    body = r.json()
    coords = body["features"][0]["geometry"]["coordinates"]
    # 6 prestart + 5 in-race = 11 positions.
    assert len(coords) == 11
    # First coord should be a prestart fix (47.65 lat range).
    assert coords[0][1] < 47.66
    # Last coord should be an in-race fix.
    assert coords[-1][1] >= 47.66


@pytest.mark.asyncio
async def test_track_excludes_prior_race_positions(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positions tagged to a different race must not bleed into this race's
    track even when they fall in the prestart window."""
    monkeypatch.setenv("AUTH_DISABLED", "true")

    # Race 1 ended 10 min before race 2's gun.
    r1_start = _GUN - timedelta(minutes=40)
    r1_end = _GUN - timedelta(minutes=10)
    r1 = await storage.start_race(
        event="CYC", start_utc=r1_start, date_str="2026-04-30", race_num=1, name="R1"
    )
    db = storage._conn()
    # 3 race-1 positions in race-2's prestart window (10 min before gun).
    for i in range(3):
        ts = (_GUN - timedelta(minutes=12) + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, 99.0, 99.0, r1.id),  # sentinel coords that should NOT appear
        )
    await db.commit()
    await storage.end_race(r1.id, r1_end)

    race2_id = await _seed_race_with_prestart(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{race2_id}/track")
    assert r.status_code == 200
    coords = r.json()["features"][0]["geometry"]["coordinates"]
    # No sentinel (99, 99) should appear.
    for _lng, lat in coords:
        assert lat != 99.0


@pytest.mark.asyncio
async def test_track_window_falls_back_when_no_race_id_tagged(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the race row has no positions tagged to its id (e.g. instrument
    data that never got the race_id stamp), the time-range path includes
    prestart by extending start_utc backwards too."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race = await storage.start_race(
        event="CYC", start_utc=_GUN, date_str="2026-04-30", race_num=1, name="R"
    )
    db = storage._conn()
    # All positions are unscoped, mixed across the prestart and the race.
    for i in range(8):
        ts = (_GUN - timedelta(minutes=2) + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 0, 47.65 + i * 0.0001, -122.40 + i * 0.0001),
        )
    await db.commit()
    await storage.end_race(race.id, _END)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{race.id}/track")
    coords = r.json()["features"][0]["geometry"]["coordinates"]
    assert len(coords) == 8
