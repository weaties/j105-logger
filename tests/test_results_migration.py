"""Schema migration v61 — race results import foundation (#459)."""

from __future__ import annotations

import pytest

from helmlog.storage import _CURRENT_VERSION, Storage


@pytest.mark.asyncio
async def test_schema_version_at_least_61(storage: Storage) -> None:
    assert _CURRENT_VERSION >= 61
    db = storage._conn()
    async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] >= 61


@pytest.mark.asyncio
async def test_regattas_table_shape(storage: Storage) -> None:
    db = storage._conn()
    async with db.execute("PRAGMA table_info(regattas)") as cur:
        cols = {r[1]: r for r in await cur.fetchall()}
    assert set(cols) == {
        "id",
        "source",
        "source_id",
        "name",
        "start_date",
        "end_date",
        "url",
        "default_class",
        "last_fetched_at",
        "created_at",
    }


@pytest.mark.asyncio
async def test_regattas_source_uniqueness(storage: Storage) -> None:
    db = storage._conn()
    await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("clubspot", "abc123", "Test Regatta", "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(Exception, match="UNIQUE"):
        await db.execute(
            "INSERT INTO regattas (source, source_id, name, created_at) VALUES (?, ?, ?, ?)",
            ("clubspot", "abc123", "Dup", "2026-01-01T00:00:00Z"),
        )
    # Same source_id under a different source is allowed.
    await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("styc", "abc123", "OK", "2026-01-01T00:00:00Z"),
    )


@pytest.mark.asyncio
async def test_series_results_table_shape(storage: Storage) -> None:
    db = storage._conn()
    async with db.execute("PRAGMA table_info(series_results)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert cols == {
        "id",
        "regatta_id",
        "boat_id",
        "class",
        "total_points",
        "net_points",
        "place_in_class",
        "place_overall",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_boats_pii_columns_added(storage: Storage) -> None:
    db = storage._conn()
    async with db.execute("PRAGMA table_info(boats)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert {"skipper", "boat_type", "phrf_rating", "yacht_club", "owner_email"} <= cols


@pytest.mark.asyncio
async def test_race_results_columns_added(storage: Storage) -> None:
    db = storage._conn()
    async with db.execute("PRAGMA table_info(race_results)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert {
        "start_time",
        "elapsed_seconds",
        "corrected_seconds",
        "points",
        "points_throwout",
        "status_code",
        "division",
        "fleet",
        # Pre-v59 columns must still exist (R23 — additive only).
        "dnf",
        "dns",
    } <= cols


@pytest.mark.asyncio
async def test_races_columns_added(storage: Storage) -> None:
    db = storage._conn()
    async with db.execute("PRAGMA table_info(races)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert {"regatta_id", "local_session_id", "source", "source_id"} <= cols


@pytest.mark.asyncio
async def test_series_results_boat_fk_cascade(storage: Storage) -> None:
    """Deleting a boat cascades series_results rows to keep the table clean."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO regattas (id, source, source_id, name, created_at) "
        "VALUES (1, 'clubspot', 'r1', 'R', '2026-01-01T00:00:00Z')"
    )
    await db.execute(
        "INSERT INTO boats (id, sail_number, name, class) VALUES (1, 'USA 123', 'Foo', 'J/105')"
    )
    await db.execute(
        "INSERT INTO series_results "
        "(regatta_id, boat_id, class, total_points, updated_at) "
        "VALUES (1, 1, 'J/105', 10.0, '2026-01-01T00:00:00Z')"
    )
    await db.execute("DELETE FROM boats WHERE id = 1")
    async with db.execute("SELECT COUNT(*) FROM series_results") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 0
