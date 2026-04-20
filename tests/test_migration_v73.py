"""Tests for migration v73 — web response cache table (#594).

Creates the `web_cache` table keyed by (key_family, race_id) for the T2
tier of the web response cache. No data migration — fresh DB schema only.
"""

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
async def test_v73_creates_web_cache_table() -> None:
    db = await _build_db_at(72)
    try:
        await _apply_migration(db, 73)
        async with db.execute("PRAGMA table_info(web_cache)") as cur:
            cols = {r[1]: r for r in await cur.fetchall()}
        assert {"key_family", "race_id", "data_hash", "blob", "created_utc"} <= cols.keys()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v73_web_cache_primary_key_is_family_plus_race() -> None:
    db = await _build_db_at(73)
    try:
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("summary", 1, "h1", "{}", "2026-04-18T00:00:00+00:00"),
        )
        await db.commit()

        # Same (family, race) collides — upsert path is exercised in cache tests.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                ("summary", 1, "h2", "{}", "2026-04-18T00:00:00+00:00"),
            )

        # Different family but same race is allowed.
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("track", 1, "h1", "{}", "2026-04-18T00:00:00+00:00"),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v73_migration_applied_on_fresh_db() -> None:
    """Confirm v73 is applied on a fresh DB.

    (Latest-version assertion lives in test_migration_v74.)
    """
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT 1 FROM schema_version WHERE version = 73") as cur:
            row = await cur.fetchone()
        assert row is not None
    finally:
        await s.close()
