"""Results importer — upsert RegattaResults into the database (#459).

Source-agnostic: takes normalized ``RegattaResults`` from any provider and
upserts into ``regattas``, ``races``, ``boats``, ``race_results``, and
``series_results`` tables in a single transaction.  Idempotent — re-importing
the same data produces zero net changes (R15, R16, R17).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    """Assign unique 1-based place numbers.

    Uses the source-provided ``place`` as the primary sort key (so
    finishes stay in the order the scoring system intended) but always
    assigns a sequential 1-based integer to guarantee the
    ``UNIQUE(race_id, place)`` constraint is satisfied.  Multiple DNC
    boats sharing the same nominal place get consecutive numbers.
    """

    def _sort_key(f: BoatFinish) -> tuple[int, int, float, str]:
        has_status = 1 if f.status_code else 0
        source_place = f.place if f.place is not None else 999
        pts = f.points if f.points is not None else 999.0
        return (has_status, source_place, pts, f.sail_number)

    ordered = sorted(finishes, key=_sort_key)
    return [(i, f) for i, f in enumerate(ordered, 1)]


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

    await _link_regatta_races_to_local_sessions(db, regatta_id)

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


def _resolve_venue_tz(venue_tz: str | None) -> tzinfo:
    """Resolve a venue timezone with sensible fallbacks.

    Order: explicit ``regatta.venue_tz`` → system local tz → UTC.
    """
    if venue_tz:
        try:
            return ZoneInfo(venue_tz)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown venue_tz {!r}, falling back", venue_tz)
    local = datetime.now().astimezone().tzinfo
    if local is not None:
        return local
    return ZoneInfo("UTC")


async def _link_regatta_races_to_local_sessions(
    db: aiosqlite.Connection,
    regatta_id: int,
) -> int:
    """Link imported races to local race sessions on the same date.

    When the number of local race-type sessions on a date is greater
    than or equal to the number of imported races on that date, imported
    races are zipped 1:1 with local sessions in order (imported by
    ``race_num``, local by ``start_utc``). This handles beer-can nights
    where race 1 and race 2 are each their own logged session.

    Otherwise (fewer local sessions than imported races, i.e. a single
    local session covered the whole sailing day), every imported race is
    linked to the earliest local session on that date — preserving the
    single-session behavior for short-handed days.

    Only rows with ``local_session_id IS NULL`` are updated, so manual
    links and prior matches are preserved.

    Returns the number of links written.
    """
    cur = await db.execute(
        "SELECT id, date, race_num, local_session_id FROM races"
        " WHERE regatta_id = ? AND source IS NOT NULL AND source != 'live'"
        " AND date IS NOT NULL",
        (regatta_id,),
    )
    imported_rows = await cur.fetchall()
    if not imported_rows:
        return 0

    by_date: dict[date, list[tuple[int, int | None, int | None]]] = {}
    for r in imported_rows:
        try:
            d = date.fromisoformat(r[1])
        except (ValueError, TypeError):
            continue
        by_date.setdefault(d, []).append((r[0], r[2], r[3]))

    linked_count = 0
    for d, imported in by_date.items():
        local_sessions = await _list_local_race_sessions_on_date(db, d)
        if not local_sessions:
            continue

        imported_sorted = sorted(
            imported,
            key=lambda row: (row[1] is None, row[1] if row[1] is not None else 0, row[0]),
        )

        zip_in_order = len(local_sessions) >= len(imported_sorted)

        for idx, (imp_id, _race_num, existing_link) in enumerate(imported_sorted):
            if existing_link is not None:
                continue
            local_id = local_sessions[idx] if zip_in_order else local_sessions[0]
            await db.execute(
                "UPDATE races SET local_session_id = ? WHERE id = ?",
                (local_id, imp_id),
            )
            logger.debug(
                "Linked imported race {} → local session {} (date {})",
                imp_id,
                local_id,
                d.isoformat(),
            )
            linked_count += 1
    return linked_count


async def _list_local_race_sessions_on_date(
    db: aiosqlite.Connection,
    target_date: date,
) -> list[int]:
    """Return local race-type session ids whose ``date`` column is *target_date*.

    Compares the stored ``date`` column directly — both helmlog's local
    race date and the importer's race date are derived from UTC, so
    matching on the string avoids venue-tz conversion bugs where a
    session near midnight could shift to a different local date than
    the imported race reports.
    """
    cur = await db.execute(
        "SELECT id, start_utc FROM races"
        " WHERE (source IS NULL OR source = 'live')"
        " AND session_type = 'race'"
        " AND date = ?"
        " ORDER BY start_utc",
        (target_date.isoformat(),),
    )
    rows = await cur.fetchall()
    return [r[0] for r in rows]


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

    # Imported races have no real start/stop timestamps — they're just
    # result rows pinned to a date. Write midnight UTC of race.date for both
    # start_utc and end_utc so (a) the NOT NULL start_utc column is
    # satisfied and (b) get_current_race() never picks them up as open
    # sessions. The previous attempt (#532) left end_utc NULL and relied on
    # a `start_utc LIKE '%T%'` filter, which matched the placeholder and
    # promoted imported rows to "current", producing ghost sessions on the
    # home page.
    placeholder_iso = f"{race.date}T00:00:00+00:00"
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, "
        "session_type, regatta_id, source, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'race', ?, ?, ?)",
        (
            f"{race.name} - {race.class_name}" if race.class_name else race.name,
            race.class_name,
            race.race_number,
            race.date,
            placeholder_iso,
            placeholder_iso,
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
