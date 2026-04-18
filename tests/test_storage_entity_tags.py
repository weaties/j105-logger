"""Tests for the generic entity-tag storage API (#587).

Covers:
- attach_tag / detach_tag / list_tags_for_entity
- list_tags(order_by='name'|'usage')
- list_entities_with_tags — decision table 2 from the /spec
- merge_tags
- ENTITY_TYPES validation
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.storage import ENTITY_TYPES

if TYPE_CHECKING:
    from helmlog.storage import Storage

_T0 = "2024-06-15T12:00:00+00:00"


async def _session(s: Storage, idx: int = 1) -> int:
    now = datetime.now(UTC).isoformat()
    assert s._db is not None
    cur = await s._db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc) VALUES (?, ?, ?, ?, ?)",
        (f"s{idx}", "E", idx, "2024-06-15", now),
    )
    await s._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# ENTITY_TYPES validation
# ---------------------------------------------------------------------------


def test_entity_types_constant() -> None:
    assert frozenset({"session", "maneuver", "thread", "bookmark", "session_note"}) == ENTITY_TYPES


@pytest.mark.asyncio
async def test_attach_rejects_unknown_entity_type(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("weather")
    with pytest.raises(ValueError, match="entity_type"):
        await storage.attach_tag("bogus", sid, tid, user_id=None)


# ---------------------------------------------------------------------------
# attach / detach / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_then_list(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("weather")
    await storage.attach_tag("session", sid, tid, user_id=None)

    tags = await storage.list_tags_for_entity("session", sid)
    assert len(tags) == 1
    assert tags[0]["id"] == tid
    assert tags[0]["name"] == "weather"


@pytest.mark.asyncio
async def test_attach_is_idempotent(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("weather")
    await storage.attach_tag("session", sid, tid, user_id=None)
    await storage.attach_tag("session", sid, tid, user_id=None)
    tags = await storage.list_tags_for_entity("session", sid)
    assert len(tags) == 1


@pytest.mark.asyncio
async def test_detach(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("weather")
    await storage.attach_tag("session", sid, tid, user_id=None)
    changed = await storage.detach_tag("session", sid, tid)
    assert changed is True
    assert await storage.list_tags_for_entity("session", sid) == []


@pytest.mark.asyncio
async def test_detach_missing_returns_false(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("nope")
    assert await storage.detach_tag("session", sid, tid) is False


# ---------------------------------------------------------------------------
# usage_count / last_used_at maintenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_increments_usage_count(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    tid = await storage.create_tag("hot")

    await storage.attach_tag("session", sid_a, tid, user_id=None)
    await storage.attach_tag("session", sid_b, tid, user_id=None)

    assert storage._db is not None
    async with storage._db.execute(
        "SELECT usage_count, last_used_at FROM tags WHERE id=?", (tid,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["usage_count"] == 2
    assert row["last_used_at"] is not None


@pytest.mark.asyncio
async def test_detach_decrements_usage_count(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("hot")
    await storage.attach_tag("session", sid, tid, user_id=None)
    await storage.detach_tag("session", sid, tid)

    assert storage._db is not None
    async with storage._db.execute("SELECT usage_count FROM tags WHERE id=?", (tid,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["usage_count"] == 0


@pytest.mark.asyncio
async def test_attach_idempotent_does_not_double_count(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("hot")
    await storage.attach_tag("session", sid, tid, user_id=None)
    await storage.attach_tag("session", sid, tid, user_id=None)

    assert storage._db is not None
    async with storage._db.execute("SELECT usage_count FROM tags WHERE id=?", (tid,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["usage_count"] == 1


# ---------------------------------------------------------------------------
# list_tags ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tags_order_by_name(storage: Storage) -> None:
    await storage.create_tag("zebra")
    await storage.create_tag("alpha")
    await storage.create_tag("mongoose")
    names = [t["name"] for t in await storage.list_tags(order_by="name")]
    assert names == ["alpha", "mongoose", "zebra"]


@pytest.mark.asyncio
async def test_list_tags_order_by_usage(storage: Storage) -> None:
    sid = await _session(storage)
    t_hot = await storage.create_tag("hot")
    await storage.create_tag("cold")
    t_warm = await storage.create_tag("warm")

    await storage.attach_tag("session", sid, t_hot, user_id=None)
    await storage.attach_tag("session", sid, t_warm, user_id=None)
    sid2 = await _session(storage, 2)
    await storage.attach_tag("session", sid2, t_hot, user_id=None)

    names = [t["name"] for t in await storage.list_tags(order_by="usage")]
    # hot (2) comes first, warm (1), cold (0)
    assert names[:2] == ["hot", "warm"]
    assert names[-1] == "cold"


# ---------------------------------------------------------------------------
# list_entities_with_tags — decision table 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_empty_returns_all(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    result = await storage.list_entities_with_tags("session", [], mode="and")
    assert set(result) == {sid_a, sid_b}


@pytest.mark.asyncio
async def test_filter_single_tag(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    tid = await storage.create_tag("x")
    await storage.attach_tag("session", sid_a, tid, user_id=None)
    result = await storage.list_entities_with_tags("session", [tid], mode="and")
    assert result == [sid_a]
    assert sid_b not in result


@pytest.mark.asyncio
async def test_filter_and_requires_all_tags(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    t1 = await storage.create_tag("t1")
    t2 = await storage.create_tag("t2")
    await storage.attach_tag("session", sid_a, t1, user_id=None)
    await storage.attach_tag("session", sid_a, t2, user_id=None)
    await storage.attach_tag("session", sid_b, t1, user_id=None)
    # sid_b has t1 only
    result = await storage.list_entities_with_tags("session", [t1, t2], mode="and")
    assert result == [sid_a]


@pytest.mark.asyncio
async def test_filter_or_matches_any(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    sid_c = await _session(storage, 3)
    t1 = await storage.create_tag("t1")
    t2 = await storage.create_tag("t2")
    await storage.attach_tag("session", sid_a, t1, user_id=None)
    await storage.attach_tag("session", sid_b, t2, user_id=None)
    result = await storage.list_entities_with_tags("session", [t1, t2], mode="or")
    assert set(result) == {sid_a, sid_b}
    assert sid_c not in result


@pytest.mark.asyncio
async def test_filter_silently_drops_unknown_tag_ids(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("t")
    await storage.attach_tag("session", sid, tid, user_id=None)
    # 9999 doesn't exist — should behave as if only [tid] was passed
    result = await storage.list_entities_with_tags("session", [tid, 9999], mode="and")
    # AND with a missing tag: entity must have the missing tag too → no match
    # Per spec: "silently dropped" → behave as [tid] only → entity matches
    assert result == [sid]


@pytest.mark.asyncio
async def test_filter_invalid_mode_raises(storage: Storage) -> None:
    with pytest.raises(ValueError, match="mode"):
        await storage.list_entities_with_tags("session", [1], mode="xor")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# merge_tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_moves_entities_to_target(storage: Storage) -> None:
    sid = await _session(storage)
    src = await storage.create_tag("src")
    tgt = await storage.create_tag("tgt")
    await storage.attach_tag("session", sid, src, user_id=None)

    await storage.merge_tags(src, tgt)

    assert await storage.list_tags_for_entity("session", sid) == [
        {"id": tgt, "name": "tgt", "color": None}
    ]
    assert storage._db is not None
    async with storage._db.execute("SELECT id FROM tags WHERE id=?", (src,)) as cur:
        row = await cur.fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_merge_deduplicates_when_entity_has_both(storage: Storage) -> None:
    sid = await _session(storage)
    src = await storage.create_tag("src")
    tgt = await storage.create_tag("tgt")
    await storage.attach_tag("session", sid, src, user_id=None)
    await storage.attach_tag("session", sid, tgt, user_id=None)

    await storage.merge_tags(src, tgt)

    tags = await storage.list_tags_for_entity("session", sid)
    assert len(tags) == 1
    assert tags[0]["id"] == tgt


@pytest.mark.asyncio
async def test_merge_recomputes_usage_count(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    src = await storage.create_tag("src")
    tgt = await storage.create_tag("tgt")
    await storage.attach_tag("session", sid_a, src, user_id=None)
    await storage.attach_tag("session", sid_b, tgt, user_id=None)

    await storage.merge_tags(src, tgt)

    assert storage._db is not None
    async with storage._db.execute("SELECT usage_count FROM tags WHERE id=?", (tgt,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["usage_count"] == 2


@pytest.mark.asyncio
async def test_merge_rejects_same_tag(storage: Storage) -> None:
    tid = await storage.create_tag("same")
    with pytest.raises(ValueError, match="itself"):
        await storage.merge_tags(tid, tid)


@pytest.mark.asyncio
async def test_merge_missing_source_raises(storage: Storage) -> None:
    tgt = await storage.create_tag("tgt")
    with pytest.raises(ValueError, match="source"):
        await storage.merge_tags(9999, tgt)


@pytest.mark.asyncio
async def test_merge_missing_target_raises(storage: Storage) -> None:
    src = await storage.create_tag("src")
    with pytest.raises(ValueError, match="target"):
        await storage.merge_tags(src, 9999)


# ---------------------------------------------------------------------------
# delete_tag cascades via entity_tags FK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_tag_cascades(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("doomed")
    await storage.attach_tag("session", sid, tid, user_id=None)
    assert storage._db is not None
    await storage._db.execute("PRAGMA foreign_keys = ON")

    assert await storage.delete_tag(tid) is True
    assert await storage.list_tags_for_entity("session", sid) == []
