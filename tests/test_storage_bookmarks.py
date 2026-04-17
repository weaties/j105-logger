"""Tests for migration v70 (Moments foundation) and bookmark CRUD.

Migration creates:
- bookmarks (new)
- tags.usage_count / tags.last_used_at (ALTER)
- entity_tags (new polymorphic join)
- comment_threads anchor columns (ALTER + backfill)

Bookmark CRUD:
- create_bookmark, list_bookmarks_for_session,
  update_bookmark, delete_bookmark, get_bookmark
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

_T0 = "2024-06-15T12:00:00+00:00"
_T1 = "2024-06-15T12:00:30+00:00"


_session_counter = 0


async def _create_session(s: Storage) -> int:
    global _session_counter
    _session_counter += 1
    now = datetime.now(UTC).isoformat()
    assert s._db is not None
    cur = await s._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"test-session-{_session_counter}", "test", _session_counter, "2024-06-15", now),
    )
    await s._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def _create_user(s: Storage, email: str = "a@example.com") -> int:
    assert s._db is not None
    cur = await s._db.execute(
        "INSERT INTO users (email, role, created_at) VALUES (?, ?, ?)",
        (email, "viewer", datetime.now(UTC).isoformat()),
    )
    await s._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Migration v70 — schema shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_version_is_70(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 70


@pytest.mark.asyncio
async def test_bookmarks_table_exists(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bookmarks'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_bookmarks_columns(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute("PRAGMA table_info(bookmarks)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    expected = {
        "id",
        "session_id",
        "created_by",
        "name",
        "note",
        "anchor_kind",
        "anchor_entity_id",
        "anchor_t_start",
        "anchor_t_end",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(cols)


@pytest.mark.asyncio
async def test_bookmarks_check_enforces_timestamp_kind(storage: Storage) -> None:
    """CHECK (anchor_kind='timestamp') is enforced at the DB layer."""
    sid = await _create_session(storage)
    assert storage._db is not None
    with pytest.raises(aiosqlite.IntegrityError):
        await storage._db.execute(
            "INSERT INTO bookmarks "
            "(session_id, name, anchor_kind, anchor_t_start, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "bad", "segment", _T0, _T0, _T0),
        )


@pytest.mark.asyncio
async def test_bookmarks_cascade_on_session_delete(storage: Storage) -> None:
    sid = await _create_session(storage)
    await storage.create_bookmark(session_id=sid, user_id=None, name="bm", note=None, t_start=_T0)
    assert storage._db is not None
    await storage._db.execute("PRAGMA foreign_keys=ON")
    await storage._db.execute("DELETE FROM races WHERE id=?", (sid,))
    await storage._db.commit()
    rows = await storage.list_bookmarks_for_session(sid)
    assert rows == []


@pytest.mark.asyncio
async def test_tags_has_new_columns(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute("PRAGMA table_info(tags)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert "usage_count" in cols
    assert "last_used_at" in cols


@pytest.mark.asyncio
async def test_entity_tags_table_exists(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_tags'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None

    async with storage._db.execute("PRAGMA table_info(entity_tags)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert cols >= {"tag_id", "entity_type", "entity_id", "created_at", "created_by"}


@pytest.mark.asyncio
async def test_comment_threads_has_anchor_columns(storage: Storage) -> None:
    assert storage._db is not None
    async with storage._db.execute("PRAGMA table_info(comment_threads)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert cols >= {
        "anchor_kind",
        "anchor_entity_id",
        "anchor_t_start",
        "anchor_t_end",
    }


# ---------------------------------------------------------------------------
# Bookmark CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_bookmark(storage: Storage) -> None:
    sid = await _create_session(storage)
    uid = await _create_user(storage)
    bm_id = await storage.create_bookmark(
        session_id=sid, user_id=uid, name="Mark 1 rounding", note="tight on layline", t_start=_T0
    )
    assert bm_id > 0

    bm = await storage.get_bookmark(bm_id)
    assert bm is not None
    assert bm["name"] == "Mark 1 rounding"
    assert bm["note"] == "tight on layline"
    assert bm["session_id"] == sid
    assert bm["created_by"] == uid
    assert bm["anchor_kind"] == "timestamp"
    assert bm["anchor_t_start"] == _T0


@pytest.mark.asyncio
async def test_list_bookmarks_orders_by_t_start(storage: Storage) -> None:
    sid = await _create_session(storage)
    uid = await _create_user(storage)
    b_late = await storage.create_bookmark(
        session_id=sid, user_id=uid, name="late", note=None, t_start=_T1
    )
    b_early = await storage.create_bookmark(
        session_id=sid, user_id=uid, name="early", note=None, t_start=_T0
    )
    rows = await storage.list_bookmarks_for_session(sid)
    ids = [r["id"] for r in rows]
    assert ids == [b_early, b_late]


@pytest.mark.asyncio
async def test_list_bookmarks_excludes_other_sessions(storage: Storage) -> None:
    sid_a = await _create_session(storage)
    sid_b = await _create_session(storage)
    uid = await _create_user(storage)
    await storage.create_bookmark(
        session_id=sid_a, user_id=uid, name="in a", note=None, t_start=_T0
    )
    await storage.create_bookmark(
        session_id=sid_b, user_id=uid, name="in b", note=None, t_start=_T0
    )
    assert len(await storage.list_bookmarks_for_session(sid_a)) == 1
    assert len(await storage.list_bookmarks_for_session(sid_b)) == 1


@pytest.mark.asyncio
async def test_update_bookmark_name_and_note(storage: Storage) -> None:
    sid = await _create_session(storage)
    uid = await _create_user(storage)
    bid = await storage.create_bookmark(
        session_id=sid, user_id=uid, name="old", note="old note", t_start=_T0
    )
    changed = await storage.update_bookmark(bid, name="new", note="new note")
    assert changed is True
    bm = await storage.get_bookmark(bid)
    assert bm is not None
    assert bm["name"] == "new"
    assert bm["note"] == "new note"
    assert bm["updated_at"] >= bm["created_at"]


@pytest.mark.asyncio
async def test_update_bookmark_note_to_null(storage: Storage) -> None:
    sid = await _create_session(storage)
    bid = await storage.create_bookmark(
        session_id=sid, user_id=None, name="n", note="has note", t_start=_T0
    )
    await storage.update_bookmark(bid, note=None, clear_note=True)
    bm = await storage.get_bookmark(bid)
    assert bm is not None
    assert bm["note"] is None


@pytest.mark.asyncio
async def test_update_missing_bookmark_returns_false(storage: Storage) -> None:
    changed = await storage.update_bookmark(9999, name="x")
    assert changed is False


@pytest.mark.asyncio
async def test_delete_bookmark(storage: Storage) -> None:
    sid = await _create_session(storage)
    bid = await storage.create_bookmark(
        session_id=sid, user_id=None, name="n", note=None, t_start=_T0
    )
    ok = await storage.delete_bookmark(bid)
    assert ok is True
    assert await storage.get_bookmark(bid) is None


@pytest.mark.asyncio
async def test_delete_missing_bookmark_returns_false(storage: Storage) -> None:
    assert await storage.delete_bookmark(9999) is False
