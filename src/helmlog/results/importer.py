"""Results importer — upsert RegattaResults into the database (#459).

Source-agnostic: takes normalized ``RegattaResults`` from any provider and
upserts into ``regattas``, ``races``, ``boats``, ``race_results``, and
``series_results`` tables in a single transaction.  Idempotent — re-importing
the same data produces zero net changes (R15, R16, R17).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import aiosqlite

    from helmlog.results.base import (
        BoatFinish,
        RaceData,
        Regatta,
        RegattaResults,
        SeriesStanding,
    )
    from helmlog.storage import Storage


def _assign_places(finishes: tuple[BoatFinish, ...]) -> list[tuple[int, BoatFinish]]:
    """Assign 1-based place numbers from points (lower is better).

    Finishes that already have an explicit ``place`` keep it.  Otherwise
    place is derived from ascending ``points`` order.  Status-code boats
    (DNF/DNS/etc.) sort after clean finishes.
    """

    def _sort_key(f: BoatFinish) -> tuple[int, float, str]:
        has_status = 1 if f.status_code else 0
        return (has_status, f.points if f.points is not None else 999.0, f.sail_number)

    ordered = sorted(finishes, key=_sort_key)
    result: list[tuple[int, BoatFinish]] = []
    for i, f in enumerate(ordered, 1):
        place = f.place if f.place is not None else i
        result.append((place, f))
    return result


async def import_results(
    storage: Storage,
    results: RegattaResults,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Upsert a ``RegattaResults`` into the database.

    Returns a summary dict with counts: ``races_upserted``,
    ``results_upserted``, ``boats_upserted``, ``standings_upserted``.
    """
    db = storage._conn()
    now = datetime.now(UTC).isoformat()
    reg = results.regatta
    counts = {
        "races_upserted": 0,
        "results_upserted": 0,
        "boats_upserted": 0,
        "standings_upserted": 0,
    }

    regatta_id = await _upsert_regatta(db, reg, now)

    boat_cache: dict[str, int] = {}

    for race_data in results.races:
        if not race_data.date:
            logger.warning(
                "Skipping race {} — no date (provider returned null)",
                race_data.source_id,
            )
            continue

        race_id = await _upsert_race(db, race_data, regatta_id, reg.source)

        ranked = _assign_places(race_data.finishes)
        for place, finish in ranked:
            boat_id = await _upsert_boat(db, finish, boat_cache)
            await _upsert_race_result(db, race_id, boat_id, finish, place, now)
            counts["results_upserted"] += 1

        counts["races_upserted"] += 1

    for standing in results.standings:
        boat_id = await _upsert_boat_minimal(db, standing.sail_number, boat_cache)
        await _upsert_series_result(db, regatta_id, boat_id, standing, now)
        counts["standings_upserted"] += 1

    await db.execute(
        "UPDATE regattas SET last_fetched_at = ? WHERE id = ?",
        (now, regatta_id),
    )

    counts["boats_upserted"] = len(boat_cache)
    await db.commit()

    await storage.log_action(
        "results_import",
        detail=json.dumps(
            {
                "regatta_id": regatta_id,
                "source": reg.source,
                "source_id": reg.source_id,
                "name": reg.name,
                **counts,
            }
        ),
        user_id=user_id,
    )

    logger.info(
        "Imported results for {!r}: {} races, {} results, {} boats, {} standings",
        reg.name,
        counts["races_upserted"],
        counts["results_upserted"],
        counts["boats_upserted"],
        counts["standings_upserted"],
    )
    return counts


async def _upsert_regatta(db: aiosqlite.Connection, reg: Regatta, now: str) -> int:
    from helmlog.results.base import Regatta

    assert isinstance(reg, Regatta)
    cur = await db.execute(
        "SELECT id FROM regattas WHERE source = ? AND source_id = ?",
        (reg.source, reg.source_id),
    )
    row = await cur.fetchone()
    if row:
        await db.execute(
            "UPDATE regattas SET name = ?, start_date = ?, end_date = ?, "
            "url = ?, default_class = ? WHERE id = ?",
            (reg.name, reg.start_date, reg.end_date, reg.url, reg.default_class, row[0]),
        )
        return row[0]  # type: ignore[no-any-return]

    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, start_date, end_date, "
        "url, default_class, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reg.source,
            reg.source_id,
            reg.name,
            reg.start_date,
            reg.end_date,
            reg.url,
            reg.default_class,
            now,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def _upsert_race(
    db: aiosqlite.Connection,
    race: RaceData,  # noqa: F821
    regatta_id: int,
    source: str,
) -> int:
    from helmlog.results.base import RaceData

    assert isinstance(race, RaceData)
    cur = await db.execute(
        "SELECT id, local_session_id FROM races WHERE source = ? AND source_id = ?",
        (source, race.source_id),
    )
    row = await cur.fetchone()
    if row:
        return row[0]  # type: ignore[no-any-return]

    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, "
        "session_type, regatta_id, source, source_id) "
        "VALUES (?, ?, ?, ?, ?, 'race', ?, ?, ?)",
        (
            race.name,
            race.class_name,
            race.race_number,
            race.date,
            race.date,
            regatta_id,
            source,
            race.source_id,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def _upsert_boat(
    db: aiosqlite.Connection,
    finish: BoatFinish,  # noqa: F821
    cache: dict[str, int],
) -> int:
    from helmlog.results.base import BoatFinish

    assert isinstance(finish, BoatFinish)
    sail = finish.sail_number
    if sail in cache:
        return cache[sail]

    cur = await db.execute("SELECT id FROM boats WHERE sail_number = ?", (sail,))
    row = await cur.fetchone()
    if row:
        boat_id: int = row[0]
        await db.execute(
            "UPDATE boats SET skipper = COALESCE(?, skipper), "
            "boat_type = COALESCE(?, boat_type), "
            "phrf_rating = COALESCE(?, phrf_rating), "
            "yacht_club = COALESCE(?, yacht_club), "
            "owner_email = COALESCE(?, owner_email), "
            "name = COALESCE(?, name) WHERE id = ?",
            (
                finish.skipper,
                finish.boat_type,
                finish.phrf_rating,
                finish.yacht_club,
                finish.owner_email,
                finish.boat_name,
                boat_id,
            ),
        )
    else:
        cur = await db.execute(
            "INSERT INTO boats (sail_number, name, class, skipper, boat_type, "
            "phrf_rating, yacht_club, owner_email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sail,
                finish.boat_name,
                finish.boat_type,
                finish.skipper,
                finish.boat_type,
                finish.phrf_rating,
                finish.yacht_club,
                finish.owner_email,
            ),
        )
        boat_id = cur.lastrowid  # type: ignore[assignment]

    cache[sail] = boat_id
    return boat_id


async def _upsert_boat_minimal(db: aiosqlite.Connection, sail: str, cache: dict[str, int]) -> int:
    if sail in cache:
        return cache[sail]
    cur = await db.execute("SELECT id FROM boats WHERE sail_number = ?", (sail,))
    row = await cur.fetchone()
    if row:
        cache[sail] = row[0]
        return row[0]  # type: ignore[no-any-return]
    cur = await db.execute("INSERT INTO boats (sail_number) VALUES (?)", (sail,))
    boat_id: int = cur.lastrowid  # type: ignore[assignment]
    cache[sail] = boat_id
    return boat_id


async def _upsert_race_result(
    db: aiosqlite.Connection,
    race_id: int,
    boat_id: int,
    finish: BoatFinish,
    place: int,
    now: str,
) -> None:
    cur = await db.execute(
        "SELECT id FROM race_results WHERE race_id = ? AND boat_id = ?",
        (race_id, boat_id),
    )
    row = await cur.fetchone()

    status = finish.status_code
    dnf = 1 if status in ("DNF", "RET") else 0
    dns = 1 if status in ("DNS", "DNC") else 0

    if row:
        await db.execute(
            "UPDATE race_results SET place = ?, finish_time = ?, "
            "start_time = ?, elapsed_seconds = ?, corrected_seconds = ?, "
            "points = ?, points_throwout = ?, status_code = ?, "
            "division = ?, fleet = ?, dnf = ?, dns = ? WHERE id = ?",
            (
                place,
                finish.finish_time,
                finish.start_time,
                finish.elapsed_seconds,
                finish.corrected_seconds,
                finish.points,
                1 if finish.points_throwout else 0,
                status,
                finish.division,
                finish.fleet,
                dnf,
                dns,
                row[0],
            ),
        )
    else:
        await db.execute(
            "INSERT INTO race_results (race_id, boat_id, place, finish_time, "
            "start_time, elapsed_seconds, corrected_seconds, points, "
            "points_throwout, status_code, division, fleet, dnf, dns, "
            "notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                race_id,
                boat_id,
                place,
                finish.finish_time,
                finish.start_time,
                finish.elapsed_seconds,
                finish.corrected_seconds,
                finish.points,
                1 if finish.points_throwout else 0,
                status,
                finish.division,
                finish.fleet,
                dnf,
                dns,
                None,
                now,
            ),
        )


async def _upsert_series_result(
    db: aiosqlite.Connection,
    regatta_id: int,
    boat_id: int,
    standing: SeriesStanding,  # noqa: F821
    now: str,
) -> None:
    cur = await db.execute(
        "SELECT id FROM series_results WHERE regatta_id = ? AND boat_id = ? AND class = ?",
        (regatta_id, boat_id, standing.class_name),
    )
    row = await cur.fetchone()
    if row:
        await db.execute(
            "UPDATE series_results SET total_points = ?, net_points = ?, "
            "place_in_class = ?, place_overall = ?, updated_at = ? WHERE id = ?",
            (
                standing.total_points,
                standing.net_points,
                standing.place_in_class,
                standing.place_overall,
                now,
                row[0],
            ),
        )
    else:
        await db.execute(
            "INSERT INTO series_results (regatta_id, boat_id, class, total_points, "
            "net_points, place_in_class, place_overall, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                regatta_id,
                boat_id,
                standing.class_name,
                standing.total_points,
                standing.net_points,
                standing.place_in_class,
                standing.place_overall,
                now,
            ),
        )
