"""Tests for migration v75 — attitudes table for heel/trim (#622)."""

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
async def test_v75_creates_attitudes_table() -> None:
    db = await _build_db_at(74)
    try:
        await _apply_migration(db, 75)
        async with db.execute("PRAGMA table_info(attitudes)") as cur:
            cols = {r[1]: r for r in await cur.fetchall()}
        assert {"id", "ts", "source_addr", "heel_deg", "trim_deg"} <= cols.keys()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v75_attitudes_ts_index_exists() -> None:
    db = await _build_db_at(75)
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'attitudes'"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        assert "idx_attitudes_ts" in names
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_version_is_75_on_fresh_db() -> None:
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 75
    finally:
        await s.close()
