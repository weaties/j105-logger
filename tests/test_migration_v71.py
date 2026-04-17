"""Tests for migration v71 — clean cutover of legacy thread anchor columns.

v71:
- Rewrites `comment_threads.title` to prepend a human-readable label when
  the row has a non-NULL `mark_reference` (so the information isn't lost).
- Drops `comment_threads.anchor_timestamp` and `mark_reference` columns.

Tested against a DB built up to v70 with legacy-shape rows pre-seeded,
then v71 applied.
"""

from __future__ import annotations

import contextlib

import aiosqlite
import pytest

from helmlog.storage import _MIGRATIONS, _split_migration_sql

_T0 = "2024-06-15T12:00:00+00:00"


async def _apply_migration(db: aiosqlite.Connection, version: int) -> None:
    """Apply a single migration, tolerating duplicate-column ADDs.

    Mirrors the same resilience as `Storage._conn().migrate()` — some ADD
    COLUMN statements in historical migrations overlap with fresh CREATE
    TABLEs, so we swallow duplicate-column errors.
    """
    for stmt in _split_migration_sql(_MIGRATIONS[version]):
        upper = stmt.lstrip().upper()
        is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
        if is_alter_add:
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute(stmt)
        else:
            await db.execute(stmt)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))
    await db.commit()


async def _build_db_at(version: int) -> aiosqlite.Connection:
    """Return an in-memory DB with migrations applied up through `version`."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for v in sorted(_MIGRATIONS):
        if v > version:
            break
        await _apply_migration(db, v)
    return db


async def _apply_v71(db: aiosqlite.Connection) -> None:
    await _apply_migration(db, 71)


async def _seed_session(db: aiosqlite.Connection) -> int:
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        ("test-s", "E", 1, "2024-06-15", _T0),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_v71_drops_legacy_columns() -> None:
    db = await _build_db_at(70)
    try:
        await _apply_v71(db)
        async with db.execute("PRAGMA table_info(comment_threads)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "anchor_timestamp" not in cols
        assert "mark_reference" not in cols
        # Anchor columns from v70 remain:
        assert {"anchor_kind", "anchor_entity_id", "anchor_t_start", "anchor_t_end"} <= cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v71_prepends_mark_label_to_existing_title() -> None:
    db = await _build_db_at(70)
    try:
        sid = await _seed_session(db)
        await db.execute(
            "INSERT INTO comment_threads "
            "(session_id, anchor_timestamp, mark_reference, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, _T0, "weather_mark_2", "Bad tack", _T0, _T0),
        )
        await db.commit()
        await _apply_v71(db)
        async with db.execute("SELECT title FROM comment_threads") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["title"] == "[weather mark 2] Bad tack"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v71_handles_null_title() -> None:
    db = await _build_db_at(70)
    try:
        sid = await _seed_session(db)
        await db.execute(
            "INSERT INTO comment_threads "
            "(session_id, mark_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, "gate_1", _T0, _T0),
        )
        await db.commit()
        await _apply_v71(db)
        async with db.execute("SELECT title FROM comment_threads") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["title"] == "[gate 1]"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v71_leaves_plain_threads_untouched() -> None:
    db = await _build_db_at(70)
    try:
        sid = await _seed_session(db)
        await db.execute(
            "INSERT INTO comment_threads "
            "(session_id, anchor_timestamp, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, _T0, "General", _T0, _T0),
        )
        await db.commit()
        await _apply_v71(db)
        async with db.execute("SELECT title FROM comment_threads") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["title"] == "General"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v71_preserves_anchor_backfill_from_v70() -> None:
    """Slice-1 backfill of anchor_t_start should survive v71 column drops."""
    # Build at v69 so the thread exists before v70's backfill runs.
    db = await _build_db_at(69)
    try:
        sid = await _seed_session(db)
        await db.execute(
            "INSERT INTO comment_threads "
            "(session_id, anchor_timestamp, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, _T0, "General", _T0, _T0),
        )
        await db.commit()

        await _apply_migration(db, 70)
        async with db.execute("SELECT anchor_kind, anchor_t_start FROM comment_threads") as cur:
            pre = await cur.fetchone()
        assert pre is not None
        assert pre["anchor_kind"] == "timestamp"
        assert pre["anchor_t_start"] == _T0

        await _apply_v71(db)
        async with db.execute("SELECT anchor_kind, anchor_t_start FROM comment_threads") as cur:
            post = await cur.fetchone()
        assert post is not None
        assert post["anchor_kind"] == "timestamp"
        assert post["anchor_t_start"] == _T0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_version_is_71_on_fresh_db() -> None:
    """Using the real Storage class (all migrations) — schema version is 71."""
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 71
    finally:
        await s.close()
