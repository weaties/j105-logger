"""Tests for migration v74 — expires_utc column on web_cache (#610)."""

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
async def test_v74_adds_expires_utc_column() -> None:
    db = await _build_db_at(73)
    try:
        await _apply_migration(db, 74)
        async with db.execute("PRAGMA table_info(web_cache)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "expires_utc" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v74_allows_null_expires_utc() -> None:
    """Existing race-keyed rows leave expires_utc NULL and rely on the
    race-mutation invalidation hook — this is explicitly allowed."""
    db = await _build_db_at(74)
    try:
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            ("session_summary", 1, "h", "{}", "2026-04-20T00:00:00+00:00"),
        )
        await db.commit()

        async with db.execute(
            "SELECT expires_utc FROM web_cache WHERE key_family = 'session_summary'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["expires_utc"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v74_migration_applied_on_fresh_db() -> None:
    """Confirm v74 is applied on a fresh DB.

    (Latest-version assertion lives in test_migration_v75.)
    """
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT 1 FROM schema_version WHERE version = 74") as cur:
            row = await cur.fetchone()
        assert row is not None
    finally:
        await s.close()
