"""Storage tests for the tag `source` / confirmation flow (#650).

Covers:
- attach_tag with source kwarg — defaults 'manual', accepts 'auto:transcript'
- list_tags_for_entity filtering of unconfirmed auto-tags (default hide,
  include with explicit flag)
- list_tags_for_entities same filtering
- confirm_tag_attachment sets confirmed_at / confirmed_by; source preserved
- manual tags always visible regardless of the flag
- Confirmed auto-tags visible regardless of the flag
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage


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


async def _user(s: Storage, email: str = "reviewer@test") -> int:
    now = datetime.now(UTC).isoformat()
    assert s._db is not None
    cur = await s._db.execute(
        "INSERT INTO users (email, role, created_at) VALUES (?, ?, ?)",
        (email, "viewer", now),
    )
    await s._db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_attach_tag_defaults_to_manual_source(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("manual-default")
    await storage.attach_tag("session", sid, tid, user_id=None)

    assert storage._db is not None
    cur = await storage._db.execute(
        "SELECT source, confirmed_at FROM entity_tags WHERE tag_id = ? AND entity_id = ?",
        (tid, sid),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["source"] == "manual"
    assert row["confirmed_at"] is None


@pytest.mark.asyncio
async def test_attach_tag_accepts_auto_source(storage: Storage) -> None:
    sid = await _session(storage)
    tid = await storage.create_tag("auto-example")
    await storage.attach_tag("session", sid, tid, user_id=None, source="auto:transcript")

    assert storage._db is not None
    cur = await storage._db.execute(
        "SELECT source, confirmed_at FROM entity_tags WHERE tag_id = ? AND entity_id = ?",
        (tid, sid),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["source"] == "auto:transcript"
    assert row["confirmed_at"] is None


@pytest.mark.asyncio
async def test_list_tags_hides_unconfirmed_auto_by_default(storage: Storage) -> None:
    """Default list excludes auto-tags pending review — they shouldn't
    pollute aggregation queries before a human has accepted them."""
    sid = await _session(storage)
    t_manual = await storage.create_tag("manual-tag")
    t_auto = await storage.create_tag("auto-tag")

    await storage.attach_tag("session", sid, t_manual, user_id=None)
    await storage.attach_tag("session", sid, t_auto, user_id=None, source="auto:transcript")

    tags = await storage.list_tags_for_entity("session", sid)
    names = {t["name"] for t in tags}
    assert "manual-tag" in names
    assert "auto-tag" not in names


@pytest.mark.asyncio
async def test_list_tags_includes_unconfirmed_when_flag_set(storage: Storage) -> None:
    sid = await _session(storage)
    t_auto = await storage.create_tag("auto-shown")
    await storage.attach_tag("session", sid, t_auto, user_id=None, source="auto:transcript")

    tags = await storage.list_tags_for_entity("session", sid, include_unconfirmed=True)
    names = {t["name"] for t in tags}
    assert "auto-shown" in names
    auto = next(t for t in tags if t["name"] == "auto-shown")
    assert auto["source"] == "auto:transcript"
    assert auto["confirmed_at"] is None


@pytest.mark.asyncio
async def test_confirm_tag_attachment(storage: Storage) -> None:
    sid = await _session(storage)
    uid = await _user(storage)
    tid = await storage.create_tag("to-confirm")
    await storage.attach_tag("session", sid, tid, user_id=None, source="auto:transcript")

    confirmed = await storage.confirm_tag_attachment("session", sid, tid, user_id=uid)
    assert confirmed is True

    assert storage._db is not None
    cur = await storage._db.execute(
        "SELECT source, confirmed_at, confirmed_by FROM entity_tags"
        " WHERE tag_id = ? AND entity_id = ?",
        (tid, sid),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["source"] == "auto:transcript", "source must be preserved — provenance is forever"
    assert row["confirmed_at"] is not None
    assert row["confirmed_by"] == uid


@pytest.mark.asyncio
async def test_confirmed_auto_tag_visible_in_default_list(storage: Storage) -> None:
    """Once a human confirms, the auto-tag becomes part of the confirmed
    dataset and shows up in default list calls."""
    sid = await _session(storage)
    uid = await _user(storage, "confirmer@test")
    tid = await storage.create_tag("confirmed-auto")
    await storage.attach_tag("session", sid, tid, user_id=None, source="auto:transcript")
    await storage.confirm_tag_attachment("session", sid, tid, user_id=uid)

    tags = await storage.list_tags_for_entity("session", sid)
    names = {t["name"] for t in tags}
    assert "confirmed-auto" in names


@pytest.mark.asyncio
async def test_list_tags_for_entities_batched_filter(storage: Storage) -> None:
    """Batched lookup must apply the same default-hide behaviour as the
    single-entity call — otherwise per-session list renders would leak
    unconfirmed auto-tags into the UI."""
    sid1 = await _session(storage, 1)
    sid2 = await _session(storage, 2)
    t_manual = await storage.create_tag("batched-manual")
    t_auto = await storage.create_tag("batched-auto")

    await storage.attach_tag("session", sid1, t_manual, user_id=None)
    await storage.attach_tag("session", sid1, t_auto, user_id=None, source="auto:detector")
    await storage.attach_tag("session", sid2, t_manual, user_id=None)

    out = await storage.list_tags_for_entities("session", [sid1, sid2])
    names1 = {t["name"] for t in out[sid1]}
    names2 = {t["name"] for t in out[sid2]}
    assert "batched-manual" in names1
    assert "batched-auto" not in names1, "unconfirmed auto-tag must be hidden by default"
    assert "batched-manual" in names2

    # With the flag, the unconfirmed auto-tag appears.
    out2 = await storage.list_tags_for_entities("session", [sid1, sid2], include_unconfirmed=True)
    assert "batched-auto" in {t["name"] for t in out2[sid1]}


@pytest.mark.asyncio
async def test_confirm_nonexistent_attachment_returns_false(storage: Storage) -> None:
    sid = await _session(storage)
    uid = await _user(storage, "ghost@test")
    tid = await storage.create_tag("not-attached")
    ok = await storage.confirm_tag_attachment("session", sid, tid, user_id=uid)
    assert ok is False
