"""Tests for migration v79 — source / confirmed_at / confirmed_by on entity_tags (#650)."""

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
async def test_v79_adds_source_and_confirmation_columns() -> None:
    db = await _build_db_at(78)
    try:
        await _apply_migration(db, 79)
        async with db.execute("PRAGMA table_info(entity_tags)") as cur:
            cols = {r[1]: r for r in await cur.fetchall()}
        assert {"source", "confirmed_at", "confirmed_by"} <= cols.keys()
        # source must be NOT NULL with DEFAULT 'manual' so existing rows
        # backfill cleanly without a data migration step.
        source_col = cols["source"]
        assert source_col[3] == 1, "source must be NOT NULL"
        assert source_col[4] == "'manual'", "source must default to 'manual'"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v79_existing_rows_backfill_to_manual() -> None:
    """An entity_tags row inserted before v79 (no source column) must end up
    with source='manual' after the migration applies."""
    db = await _build_db_at(78)
    try:
        # Seed a tag + a legacy entity_tag row (no source column yet).
        await db.execute(
            "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
            ("legacy", "#000000", "2026-01-01T00:00:00"),
        )
        row = await (await db.execute("SELECT id FROM tags WHERE name = 'legacy'")).fetchone()
        assert row is not None
        tag_id = row[0]
        await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
            ("legacy-s", "E", 1, "2026-01-01", "2026-01-01T00:00:00"),
        )
        session_row = await (await db.execute("SELECT last_insert_rowid()")).fetchone()
        assert session_row is not None
        session_id = session_row[0]
        await db.execute(
            "INSERT INTO entity_tags (tag_id, entity_type, entity_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (tag_id, "session", session_id, "2026-01-01T00:00:00"),
        )
        await db.commit()

        await _apply_migration(db, 79)

        cur = await db.execute(
            "SELECT source, confirmed_at, confirmed_by FROM entity_tags WHERE tag_id = ?",
            (tag_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "manual"
        assert row[1] is None
        assert row[2] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v79_creates_source_index() -> None:
    db = await _build_db_at(79)
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'entity_tags'"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        assert "idx_entity_tags_source" in names
    finally:
        await db.close()


# Fresh-DB schema_version is asserted dynamically against _CURRENT_VERSION
# in test_migration_v75.py::test_schema_version_is_current_on_fresh_db.
