"""Tests for migration v72 — entity_tags cutover for #587 / slice 3.

v72:
- Copies rows from `session_tags` to `entity_tags` with entity_type='session'.
- Copies rows from `note_tags` to `entity_tags` with entity_type='session_note'.
- Backfills `tags.usage_count` and `tags.last_used_at` from the combined rows.
- Drops `session_tags` and `note_tags` tables.
"""

from __future__ import annotations

import contextlib

import aiosqlite
import pytest

from helmlog.storage import _MIGRATIONS, _split_migration_sql

_T0 = "2024-06-15T12:00:00+00:00"


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


async def _seed_session(db: aiosqlite.Connection, idx: int = 1) -> int:
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"s{idx}", "E", idx, "2024-06-15", _T0),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def _seed_note(db: aiosqlite.Connection, sid: int) -> int:
    cur = await db.execute(
        "INSERT INTO session_notes (race_id, ts, note_type, body, created_at) "
        "VALUES (?, ?, 'freeform', 'note', ?)",
        (sid, _T0, _T0),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def _seed_tag(db: aiosqlite.Connection, name: str) -> int:
    cur = await db.execute("INSERT INTO tags (name, created_at) VALUES (?, ?)", (name, _T0))
    await db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_v72_drops_legacy_tag_join_tables() -> None:
    db = await _build_db_at(71)
    try:
        await _apply_migration(db, 72)
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('session_tags', 'note_tags')"
        ) as cur:
            rows = await cur.fetchall()
        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v72_migrates_session_tags_to_entity_tags() -> None:
    db = await _build_db_at(71)
    try:
        sid = await _seed_session(db)
        tid = await _seed_tag(db, "weather")
        await db.execute("INSERT INTO session_tags (session_id, tag_id) VALUES (?, ?)", (sid, tid))
        await db.commit()

        await _apply_migration(db, 72)

        async with db.execute("SELECT entity_type, entity_id, tag_id FROM entity_tags") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        assert rows == [{"entity_type": "session", "entity_id": sid, "tag_id": tid}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v72_migrates_note_tags_to_entity_tags() -> None:
    db = await _build_db_at(71)
    try:
        sid = await _seed_session(db)
        nid = await _seed_note(db, sid)
        tid = await _seed_tag(db, "debrief")
        await db.execute("INSERT INTO note_tags (note_id, tag_id) VALUES (?, ?)", (nid, tid))
        await db.commit()

        await _apply_migration(db, 72)

        async with db.execute("SELECT entity_type, entity_id, tag_id FROM entity_tags") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        assert rows == [{"entity_type": "session_note", "entity_id": nid, "tag_id": tid}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v72_backfills_tag_usage_count() -> None:
    db = await _build_db_at(71)
    try:
        sid1 = await _seed_session(db, 1)
        sid2 = await _seed_session(db, 2)
        nid = await _seed_note(db, sid1)
        tid = await _seed_tag(db, "hot")
        await db.execute("INSERT INTO session_tags (session_id, tag_id) VALUES (?, ?)", (sid1, tid))
        await db.execute("INSERT INTO session_tags (session_id, tag_id) VALUES (?, ?)", (sid2, tid))
        await db.execute("INSERT INTO note_tags (note_id, tag_id) VALUES (?, ?)", (nid, tid))
        await db.commit()

        await _apply_migration(db, 72)

        async with db.execute("SELECT usage_count FROM tags WHERE id=?", (tid,)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["usage_count"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v72_leaves_untagged_tag_with_usage_zero() -> None:
    db = await _build_db_at(71)
    try:
        _unused = await _seed_tag(db, "dormant")
        await _apply_migration(db, 72)
        async with db.execute("SELECT usage_count FROM tags WHERE name='dormant'") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["usage_count"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v72_migration_applied_on_fresh_db() -> None:
    """Confirm v72 is applied on a fresh DB.

    (Latest-version assertion lives in test_migration_v73.)
    """
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT 1 FROM schema_version WHERE version = 72") as cur:
            row = await cur.fetchone()
        assert row is not None
    finally:
        await s.close()
