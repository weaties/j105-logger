"""Tests for migration v80 — moments unification (#662).

Covers the schema-level DDL (new tables exist, expected columns/indexes) AND
the Python data migration that copies comment_threads / session_notes data
forward into moments + attachments + session_settings and drops the old
tables. The data-migration cases seed the legacy tables at v79, then run the
v80 migration and assert the new tables carry the data over.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from helmlog.storage import (
    _MIGRATIONS,
    Storage,
    StorageConfig,
    _split_migration_sql,
)

if TYPE_CHECKING:
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# DDL-only assertions (schema shape after the v80 migration string runs)
# ---------------------------------------------------------------------------


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
async def test_v80_indexes_present() -> None:
    db = await _build_db_at(80)
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name IN ('moments', 'moment_attachments', 'session_settings')"
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
        }
        missing = expected - names
        assert not missing, f"indexes missing: {sorted(missing)}"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Data-migration assertions: seed legacy tables at v79, run v80 end-to-end.
# ---------------------------------------------------------------------------


async def _storage_at_v79(tmp_path: Path) -> Storage:  # type: ignore[no-untyped-def]
    """Build a Storage whose schema is frozen at v79 so we can seed the
    legacy comment_threads / session_notes rows before running v80. Bypasses
    Storage.connect()'s auto-migrate so we don't jump straight to v80."""
    db_path = tmp_path / "v79.db"
    storage = Storage(StorageConfig(db_path=str(db_path)))
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    storage._db = db  # type: ignore[attr-defined]

    await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    await db.commit()
    for v in sorted(_MIGRATIONS):
        if v > 79:
            break
        for stmt in _split_migration_sql(_MIGRATIONS[v]):
            upper = stmt.lstrip().upper()
            is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
            if is_alter_add:
                with contextlib.suppress(Exception):
                    await db.execute(stmt)
            else:
                await db.execute(stmt)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (v,))
    await db.commit()
    return storage


async def _finish_to_v80(storage: Storage) -> None:
    """Run the v80 DDL + data migration on a storage frozen at v79."""
    db = storage._conn()
    for stmt in _split_migration_sql(_MIGRATIONS[80]):
        await db.execute(stmt)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (80)")
    await db.commit()
    await storage._migrate_v80_moments()


@pytest.mark.asyncio
async def test_v80_copies_comment_threads_to_moments(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    # Seed: one race, one thread, two comments, a read state row.
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc,"
        " end_utc, slug) VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', '2026-01-01T13:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO users (id, email, name, role, created_at)"
        " VALUES (1, 'a@b.co', 'Alice', 'admin', '2026-01-01T00:00:00+00:00')"
    )
    await db.execute(
        "INSERT INTO comment_threads"
        " (id, session_id, title, anchor_kind, anchor_t_start,"
        "  created_by, created_at, updated_at, resolved)"
        " VALUES (10, 1, 'Big luff', 'timestamp', '2026-01-01T12:05:00+00:00',"
        "         1, '2026-01-01T12:05:00+00:00', '2026-01-01T12:05:00+00:00', 0)"
    )
    await db.execute(
        "INSERT INTO comments (id, thread_id, author, body, created_at)"
        " VALUES (100, 10, 1, 'nice lift', '2026-01-01T12:06:00+00:00')"
    )
    await db.execute(
        "INSERT INTO comments (id, thread_id, author, body, created_at)"
        " VALUES (101, 10, 1, 'but we lost it', '2026-01-01T12:07:00+00:00')"
    )
    await db.execute(
        "INSERT INTO comment_read_state (user_id, thread_id, last_read)"
        " VALUES (1, 10, '2026-01-01T12:10:00+00:00')"
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute("SELECT id, session_id, subject, anchor_kind FROM moments")
    moments = [dict(r) for r in await cur.fetchall()]
    assert len(moments) == 1
    m = moments[0]
    assert m["session_id"] == 1
    assert m["subject"] == "Big luff"
    assert m["anchor_kind"] == "timestamp"

    cur = await db.execute("SELECT moment_id, body FROM comments ORDER BY created_at")
    cm = [dict(r) for r in await cur.fetchall()]
    assert len(cm) == 2
    assert all(c["moment_id"] == m["id"] for c in cm)
    assert [c["body"] for c in cm] == ["nice lift", "but we lost it"]

    cur = await db.execute("SELECT moment_id FROM comment_read_state WHERE user_id = 1")
    rs = await cur.fetchone()
    assert rs is not None and rs["moment_id"] == m["id"]

    for gone in ("comment_threads", "comments_new", "comment_read_state_new", "bookmarks"):
        cur = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (gone,))
        assert await cur.fetchone() is None, f"{gone} should be gone after v80"

    await storage.close()


@pytest.mark.asyncio
async def test_v80_migrates_settings_notes_to_session_settings(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, slug)"
        " VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO session_notes"
        " (race_id, ts, note_type, body, created_at)"
        " VALUES (1, '2026-01-01T12:00:00+00:00', 'settings',"
        '         \'{"backstay": 5, "cunningham": 3}\','
        "         '2026-01-01T12:00:00+00:00')"
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute("SELECT session_id, body FROM session_settings")
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == 1
    assert "backstay" in rows[0]["body"]

    cur = await db.execute("SELECT COUNT(*) AS n FROM moments")
    row = await cur.fetchone()
    assert row is not None and row["n"] == 0  # settings don't become moments

    await storage.close()


@pytest.mark.asyncio
async def test_v80_migrates_text_and_photo_notes_to_moments(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, slug)"
        " VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO session_notes"
        " (race_id, ts, note_type, body, created_at)"
        " VALUES (1, '2026-01-01T12:10:00+00:00', 'text', 'wind shift right',"
        "         '2026-01-01T12:10:00+00:00')"
    )
    await db.execute(
        "INSERT INTO session_notes"
        " (race_id, ts, note_type, body, photo_path, created_at)"
        " VALUES (1, '2026-01-01T12:15:00+00:00', 'photo',"
        "         'caption here', '1/photo-xyz.jpg',"
        "         '2026-01-01T12:15:00+00:00')"
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute(
        "SELECT session_id, anchor_kind, anchor_t_start FROM moments ORDER BY id"
    )
    moments = [dict(r) for r in await cur.fetchall()]
    assert len(moments) == 2
    assert {m["anchor_kind"] for m in moments} == {"timestamp"}

    cur = await db.execute("SELECT kind, path FROM moment_attachments")
    atts = [dict(r) for r in await cur.fetchall()]
    assert len(atts) == 1
    assert atts[0]["kind"] == "photo"
    assert atts[0]["path"] == "1/photo-xyz.jpg"

    cur = await db.execute("SELECT body FROM comments ORDER BY id")
    bodies = [r["body"] for r in await cur.fetchall()]
    # text note → comment, photo caption → comment
    assert bodies == ["wind shift right", "caption here"]

    await storage.close()


@pytest.mark.asyncio
async def test_v80_skips_audio_only_notes(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO audio_sessions"
        " (id, file_path, device_name, start_utc, sample_rate, channels)"
        " VALUES (7, '/audio/x.wav', 'dev0',"
        "         '2026-01-01T12:00:00+00:00', 48000, 1)"
    )
    await db.execute(
        "INSERT INTO session_notes"
        " (race_id, audio_session_id, ts, note_type, body, created_at)"
        " VALUES (NULL, 7, '2026-01-01T12:00:00+00:00', 'text', 'floating',"
        "         '2026-01-01T12:00:00+00:00')"
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute("SELECT COUNT(*) AS n FROM moments")
    row = await cur.fetchone()
    assert row is not None and row["n"] == 0

    await storage.close()


@pytest.mark.asyncio
async def test_v80_remaps_entity_tags(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, slug)"
        " VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO users (id, email, name, role, created_at)"
        " VALUES (1, 'a@b.co', 'Alice', 'admin', '2026-01-01T00:00:00+00:00')"
    )
    # A tag with id=1 is already seeded by v76's starter vocabulary; grab it.
    cur = await db.execute("SELECT id FROM tags ORDER BY id LIMIT 1")
    tag_row = await cur.fetchone()
    assert tag_row is not None
    tag_id = int(tag_row["id"])
    await db.execute(
        "INSERT INTO comment_threads"
        " (id, session_id, title, anchor_kind, anchor_t_start,"
        "  created_by, created_at, updated_at, resolved)"
        " VALUES (42, 1, 'X', 'timestamp', '2026-01-01T12:00:00+00:00',"
        "         1, '2026-01-01T12:00:00+00:00', '2026-01-01T12:00:00+00:00', 0)"
    )
    await db.execute(
        "INSERT INTO entity_tags (tag_id, entity_type, entity_id, created_at)"
        " VALUES (?, 'thread', 42, '2026-01-01T00:00:00+00:00')",
        (tag_id,),
    )
    await db.execute(
        "INSERT INTO entity_tags (tag_id, entity_type, entity_id, created_at)"
        " VALUES (?, 'bookmark', 99, '2026-01-01T00:00:00+00:00')",
        (tag_id,),
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute("SELECT entity_type, entity_id FROM entity_tags ORDER BY rowid")
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1  # bookmark row dropped, thread remapped
    assert rows[0]["entity_type"] == "moment"
    # The moment was inserted from thread 42; confirm entity_id matches a moment.
    cur = await db.execute("SELECT id FROM moments")
    mids = {int(r["id"]) for r in await cur.fetchall()}
    assert rows[0]["entity_id"] in mids

    await storage.close()


@pytest.mark.asyncio
async def test_v80_remaps_notifications_source(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, slug)"
        " VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO users (id, email, name, role, created_at)"
        " VALUES (1, 'a@b.co', 'Alice', 'admin', '2026-01-01T00:00:00+00:00')"
    )
    await db.execute(
        "INSERT INTO comment_threads"
        " (id, session_id, title, anchor_kind, anchor_t_start,"
        "  created_by, created_at, updated_at, resolved)"
        " VALUES (55, 1, 'Y', 'timestamp', '2026-01-01T12:00:00+00:00',"
        "         1, '2026-01-01T12:00:00+00:00', '2026-01-01T12:00:00+00:00', 0)"
    )
    await db.execute(
        "INSERT INTO notifications"
        " (user_id, type, source_thread_id, session_id, actor_id,"
        "  message, created_at)"
        " VALUES (1, 'new_thread', 55, 1, 1, 'started a discussion',"
        "         '2026-01-01T12:01:00+00:00')"
    )
    await db.commit()

    await _finish_to_v80(storage)

    cur = await db.execute("SELECT type, source_moment_id FROM notifications")
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["type"] == "new_thread"
    cur = await db.execute("SELECT id FROM moments")
    mids = {int(r["id"]) for r in await cur.fetchall()}
    assert rows[0]["source_moment_id"] in mids

    await storage.close()


@pytest.mark.asyncio
async def test_v80_is_idempotent(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = await _storage_at_v79(tmp_path)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, slug)"
        " VALUES (1, 'Race 1', 'Test', 1, '2026-01-01',"
        " '2026-01-01T12:00:00+00:00', 'race-1')"
    )
    await db.execute(
        "INSERT INTO users (id, email, name, role, created_at)"
        " VALUES (1, 'a@b.co', 'Alice', 'admin', '2026-01-01T00:00:00+00:00')"
    )
    await db.execute(
        "INSERT INTO comment_threads"
        " (id, session_id, title, anchor_kind, anchor_t_start,"
        "  created_by, created_at, updated_at, resolved)"
        " VALUES (1, 1, 'only', 'timestamp', '2026-01-01T12:00:00+00:00',"
        "         1, '2026-01-01T12:00:00+00:00', '2026-01-01T12:00:00+00:00', 0)"
    )
    await db.commit()

    await _finish_to_v80(storage)
    # Re-running the data migration must not duplicate rows.
    await storage._migrate_v80_moments()

    cur = await db.execute("SELECT COUNT(*) AS n FROM moments")
    row = await cur.fetchone()
    assert row is not None and row["n"] == 1

    await storage.close()
