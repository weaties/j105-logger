"""Tests for migration v80 — moments unification (#662).

This test covers the schema-level DDL only. The Python data migration
(_migrate_v80_moments), which copies comment_threads / session_notes
data into moments + attachments and drops the old tables, is not yet
wired up — those assertions arrive in the follow-up commit that adds
the data migration.
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
async def test_v80_creates_moments_table() -> None:
    db = await _build_db_at(80)
    try:
        async with db.execute("PRAGMA table_info(moments)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        expected = {
            "id",
            "session_id",
            "subject",
            "counterparty",
            "anchor_kind",
            "anchor_entity_id",
            "anchor_t_start",
            "anchor_t_end",
            "resolved",
            "resolved_at",
            "resolved_by",
            "resolution_summary",
            "source",
            "confirmed_at",
            "confirmed_by",
            "created_by",
            "created_at",
            "updated_at",
        }
        missing = expected - cols
        assert not missing, f"moments missing columns: {sorted(missing)}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_creates_moment_attachments() -> None:
    db = await _build_db_at(80)
    try:
        async with db.execute("PRAGMA table_info(moment_attachments)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert {"id", "moment_id", "kind", "path", "body", "created_by", "created_at"} <= cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_creates_session_settings() -> None:
    """session_settings rescues note_type='settings' rows from the
    collapsing session_notes table. It's a config snapshot store, not a
    human-authored note."""
    db = await _build_db_at(80)
    try:
        async with db.execute("PRAGMA table_info(session_settings)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert {
            "id",
            "session_id",
            "audio_session_id",
            "ts",
            "body",
            "created_by",
            "created_at",
        } <= cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_creates_comments_new_staging() -> None:
    """comments_new / comment_read_state_new are staging tables. The
    Python data migration (_migrate_v80_moments, follow-up commit) copies
    from the old comments / comment_read_state into them, then drops the
    originals and renames."""
    db = await _build_db_at(80)
    try:
        async with db.execute("PRAGMA table_info(comments_new)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert {"id", "moment_id", "author", "body", "created_at", "edited_at"} <= cols

        async with db.execute("PRAGMA table_info(comment_read_state_new)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert {"user_id", "moment_id", "last_read"} <= cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_indexes_present() -> None:
    db = await _build_db_at(80)
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name IN ('moments', 'moment_attachments', 'session_settings', 'comments_new')"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        expected = {
            "idx_moments_session_ts",
            "idx_moments_session_kind",
            "idx_moments_counterparty",
            "idx_moments_source",
            "idx_moments_open",
            "idx_moment_attachments_moment",
            "idx_session_settings_session",
            "idx_comments_new_moment",
        }
        missing = expected - names
        assert not missing, f"indexes missing: {sorted(missing)}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v80_old_tables_still_exist() -> None:
    """The DDL migration is non-destructive: old tables remain alive
    until the Python data migration (_migrate_v80_moments, follow-up
    commit) copies data forward and drops them."""
    db = await _build_db_at(80)
    try:
        async with db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
            tables = {r[0] for r in await cur.fetchall()}
        for old in ("bookmarks", "session_notes", "comment_threads", "comments"):
            assert old in tables, f"{old} should still exist after v80 DDL"
    finally:
        await db.close()
