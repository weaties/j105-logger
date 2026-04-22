"""Tests for migration v80 — counterparty column on bookmarks (#651)."""

from __future__ import annotations

import contextlib

import aiosqlite
import pytest

from helmlog.storage import _MIGRATIONS, _split_migration_sql


async def _apply_migration(db: aiosqlite.Connection, version: int) -> None:
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
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for v in sorted(_MIGRATIONS):
        if v > version:
            break
        await _apply_migration(db, v)
    return db


@pytest.mark.asyncio
async def test_v80_adds_counterparty_column() -> None:
    db = await _build_db_at(79)
    try:
        await _apply_migration(db, 80)
        async with db.execute("PRAGMA table_info(bookmarks)") as cur:
            cols = {r[1]: r for r in await cur.fetchall()}
        assert "counterparty" in cols
        # Must be nullable — most bookmarks won't have a counterparty.
        assert cols["counterparty"][3] == 0, "counterparty must be nullable"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_creates_partial_index() -> None:
    """Partial index keeps the common 'no counterparty' case out of the
    index entirely — indexed entries only exist for rows that actually
    have a counterparty to look up."""
    db = await _build_db_at(80)
    try:
        async with db.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type = 'index' AND name = 'idx_bookmarks_counterparty'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert "WHERE" in row[0].upper(), "index must be partial"
        assert "counterparty IS NOT NULL" in row[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_existing_bookmarks_unaffected() -> None:
    """A bookmark created before v80 must still read cleanly; counterparty
    is simply NULL on legacy rows."""
    db = await _build_db_at(79)
    try:
        await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
            ("legacy", "E", 1, "2026-01-01", "2026-01-01T00:00:00"),
        )
        sid_row = await (await db.execute("SELECT last_insert_rowid()")).fetchone()
        assert sid_row is not None
        sid = sid_row[0]
        await db.execute(
            "INSERT INTO bookmarks"
            " (session_id, name, anchor_kind, anchor_t_start, created_at, updated_at)"
            " VALUES (?, 'old', 'timestamp', '2026-01-01T00:00:30', '2026-01-01', '2026-01-01')",
            (sid,),
        )
        await db.commit()

        await _apply_migration(db, 80)

        cur = await db.execute("SELECT counterparty FROM bookmarks WHERE session_id = ?", (sid,))
        row = await cur.fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        await db.close()


# Fresh-DB schema_version is asserted dynamically against _CURRENT_VERSION
# in test_migration_v75.py::test_schema_version_is_current_on_fresh_db.
