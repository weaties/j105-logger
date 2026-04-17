"""Storage tests for anchored thread CRUD + anchor-picker data source (#478)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.anchors import Anchor
from helmlog.storage import AnchorScopeError

if TYPE_CHECKING:
    from helmlog.storage import Storage

_T0 = "2024-06-15T12:00:00+00:00"
_T1 = "2024-06-15T12:00:30+00:00"


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


async def _user(s: Storage, uid: int) -> None:
    assert s._db is not None
    await s._db.execute(
        "INSERT INTO users (id, email, role, created_at) VALUES (?, ?, 'viewer', ?)",
        (uid, f"u{uid}@e.com", _T0),
    )
    await s._db.commit()


async def _maneuver(s: Storage, session_id: int) -> int:
    assert s._db is not None
    cur = await s._db.execute(
        "INSERT INTO maneuvers (session_id, type, ts, end_ts) VALUES (?, 'tack', ?, ?)",
        (session_id, _T0, _T1),
    )
    await s._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# create_comment_thread with new `anchor` kwarg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_thread_with_timestamp_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    tid = await storage.create_comment_thread(
        sid, 1, anchor=Anchor(kind="timestamp", t_start=_T0), title="Layline call"
    )
    thread = await storage.get_comment_thread(tid)
    assert thread is not None
    assert thread["anchor"] == {"kind": "timestamp", "t_start": _T0}


@pytest.mark.asyncio
async def test_create_thread_with_no_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    tid = await storage.create_comment_thread(sid, 1, title="General discussion")
    thread = await storage.get_comment_thread(tid)
    assert thread is not None
    assert thread["anchor"] is None


@pytest.mark.asyncio
async def test_create_thread_with_maneuver_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    mid = await _maneuver(storage, sid)
    tid = await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="maneuver", entity_id=mid))
    thread = await storage.get_comment_thread(tid)
    assert thread is not None
    assert thread["anchor"] == {"kind": "maneuver", "entity_id": mid}


@pytest.mark.asyncio
async def test_create_thread_with_bookmark_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    bid = await storage.create_bookmark(
        session_id=sid, user_id=1, name="bad call", note=None, t_start=_T0
    )
    tid = await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="bookmark", entity_id=bid))
    thread = await storage.get_comment_thread(tid)
    assert thread is not None
    assert thread["anchor"] == {"kind": "bookmark", "entity_id": bid}


@pytest.mark.asyncio
async def test_create_thread_with_race_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    tid = await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="race", entity_id=sid))
    thread = await storage.get_comment_thread(tid)
    assert thread is not None
    assert thread["anchor"] == {"kind": "race", "entity_id": sid}


# ---------------------------------------------------------------------------
# Cross-entity scoping (decision table 1 rule)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maneuver_anchor_must_belong_to_session(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    await _user(storage, 1)
    mid_in_b = await _maneuver(storage, sid_b)

    with pytest.raises(AnchorScopeError, match="maneuver"):
        await storage.create_comment_thread(
            sid_a, 1, anchor=Anchor(kind="maneuver", entity_id=mid_in_b)
        )


@pytest.mark.asyncio
async def test_bookmark_anchor_must_belong_to_session(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    await _user(storage, 1)
    bid_in_b = await storage.create_bookmark(
        session_id=sid_b, user_id=1, name="x", note=None, t_start=_T0
    )
    with pytest.raises(AnchorScopeError, match="bookmark"):
        await storage.create_comment_thread(
            sid_a, 1, anchor=Anchor(kind="bookmark", entity_id=bid_in_b)
        )


@pytest.mark.asyncio
async def test_race_anchor_entity_id_must_equal_session(storage: Storage) -> None:
    sid_a = await _session(storage, 1)
    sid_b = await _session(storage, 2)
    await _user(storage, 1)
    with pytest.raises(AnchorScopeError, match="race"):
        await storage.create_comment_thread(sid_a, 1, anchor=Anchor(kind="race", entity_id=sid_b))


@pytest.mark.asyncio
async def test_rounding_kind_rejected_for_threads(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    with pytest.raises(AnchorScopeError, match="rounding"):
        await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="rounding", entity_id=1))


@pytest.mark.asyncio
async def test_missing_entity_rejected(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    with pytest.raises(AnchorScopeError, match="maneuver"):
        await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="maneuver", entity_id=9999))


# ---------------------------------------------------------------------------
# list_comment_threads projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threads_projects_anchor(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    await storage.create_comment_thread(sid, 1, anchor=Anchor(kind="timestamp", t_start=_T0))
    await storage.create_comment_thread(sid, 1, title="No anchor")

    threads = await storage.list_comment_threads(sid, 1)
    assert len(threads) == 2
    anchors = [t["anchor"] for t in threads]
    assert {"kind": "timestamp", "t_start": _T0} in anchors
    assert None in anchors


# ---------------------------------------------------------------------------
# list_session_anchors (picker data source)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_session_anchors_includes_race_and_start(storage: Storage) -> None:
    sid = await _session(storage)
    anchors = await storage.list_session_anchors(sid)
    kinds = {a["kind"] for a in anchors}
    assert "race" in kinds
    assert "start" in kinds


@pytest.mark.asyncio
async def test_list_session_anchors_includes_maneuvers_and_bookmarks(
    storage: Storage,
) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    mid = await _maneuver(storage, sid)
    bid = await storage.create_bookmark(
        session_id=sid, user_id=1, name="spot", note=None, t_start=_T1
    )
    anchors = await storage.list_session_anchors(sid)
    by_kind_id = {(a["kind"], a["entity_id"]) for a in anchors}
    assert ("maneuver", mid) in by_kind_id
    assert ("bookmark", bid) in by_kind_id


@pytest.mark.asyncio
async def test_list_session_anchors_orders_by_t_start(storage: Storage) -> None:
    sid = await _session(storage)
    await _user(storage, 1)
    # Bookmark at later time; maneuver earlier. Race+start are at session start.
    await storage.create_bookmark(
        session_id=sid, user_id=1, name="late", note=None, t_start="2024-06-15T12:05:00+00:00"
    )
    anchors = await storage.list_session_anchors(sid)
    starts = [a["t_start"] for a in anchors]
    assert starts == sorted(starts)


@pytest.mark.asyncio
async def test_list_session_anchors_empty_for_missing_session(storage: Storage) -> None:
    assert await storage.list_session_anchors(9999) == []
