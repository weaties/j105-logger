"""Tests for Vakaros -> races session matching (#458).

Model: one Vakaros session can link to *many* races. For each race whose
time window overlaps the Vakaros session by at least 50% of the shorter
duration, ``races.vakaros_session_id`` is set to the Vakaros session id.
Races still in progress (end_utc IS NULL) are never matched. Races that
already point at a different Vakaros session are left alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.vakaros import PositionRow, VakarosSession

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _insert_race(
    storage: Storage,
    name: str,
    start: datetime,
    end: datetime | None,
) -> int:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            "test-event",
            1,
            start.date().isoformat(),
            start.isoformat(),
            end.isoformat() if end else None,
            "race",
        ),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def _race_vakaros_link(storage: Storage, race_id: int) -> int | None:
    """Look up a race's vakaros_session_id directly."""
    db = storage._conn()
    cur = await db.execute("SELECT vakaros_session_id FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    return int(row["vakaros_session_id"]) if row and row["vakaros_session_id"] else None


def _make_vakaros_session(
    start: datetime, end: datetime, source_hash: str = "a" * 64
) -> VakarosSession:
    return VakarosSession(
        source_hash=source_hash,
        source_file="test.vkx",
        start_utc=start,
        end_utc=end,
        positions=(
            PositionRow(
                timestamp=start,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=end,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(),
        race_events=(),
        winds=(),
    )


@pytest.mark.asyncio
async def test_match_returns_empty_list_when_no_races_exist(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    linked = await storage.match_vakaros_session(session_id)
    assert linked == []


@pytest.mark.asyncio
async def test_match_links_race_that_is_fully_inside_vakaros_window(
    storage: Storage,
) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(
        storage, "Race 1", t0 + timedelta(minutes=30), t0 + timedelta(minutes=90)
    )

    linked = await storage.match_vakaros_session(session_id)
    assert linked == [race_id]
    assert await _race_vakaros_link(storage, race_id) == session_id


@pytest.mark.asyncio
async def test_match_links_all_overlapping_races_in_one_vakaros_session(
    storage: Storage,
) -> None:
    """Real-world case: one VKX file spans multiple races + practice."""
    t0 = datetime(2026, 4, 9, 0, 42, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    practice_id = await _insert_race(
        storage, "Practice", t0 + timedelta(minutes=13), t0 + timedelta(minutes=27)
    )
    race1_id = await _insert_race(
        storage, "Race 1", t0 + timedelta(minutes=48), t0 + timedelta(minutes=85)
    )
    race2_id = await _insert_race(
        storage, "Race 2", t0 + timedelta(minutes=90), t0 + timedelta(minutes=113)
    )

    linked = await storage.match_vakaros_session(session_id)
    assert set(linked) == {practice_id, race1_id, race2_id}

    for rid in (practice_id, race1_id, race2_id):
        assert await _race_vakaros_link(storage, rid) == session_id


@pytest.mark.asyncio
async def test_match_rejects_race_with_less_than_50_percent_overlap(
    storage: Storage,
) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=1))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(
        storage, "Race far", t0 + timedelta(minutes=50), t0 + timedelta(minutes=110)
    )

    linked = await storage.match_vakaros_session(session_id)
    assert linked == []
    assert await _race_vakaros_link(storage, race_id) is None


@pytest.mark.asyncio
async def test_match_ignores_race_with_null_end_utc(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(storage, "In-progress race", t0 + timedelta(minutes=10), None)

    linked = await storage.match_vakaros_session(session_id)
    assert linked == []
    assert await _race_vakaros_link(storage, race_id) is None


@pytest.mark.asyncio
async def test_match_does_not_steal_race_from_other_session(
    storage: Storage,
) -> None:
    """A race already linked to a different Vakaros session is left alone."""
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session_a = _make_vakaros_session(t0, t0 + timedelta(hours=2), source_hash="a" * 64)
    session_b = _make_vakaros_session(t0, t0 + timedelta(hours=2), source_hash="b" * 64)
    id_a = await storage.store_vakaros_session(session_a)
    id_b = await storage.store_vakaros_session(session_b)

    race_id = await _insert_race(
        storage, "Race", t0 + timedelta(minutes=30), t0 + timedelta(minutes=90)
    )

    assert await storage.match_vakaros_session(id_a) == [race_id]
    assert await storage.match_vakaros_session(id_b) == []
    assert await _race_vakaros_link(storage, race_id) == id_a


@pytest.mark.asyncio
async def test_match_is_idempotent(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(
        storage, "Race", t0 + timedelta(minutes=30), t0 + timedelta(minutes=90)
    )

    first = await storage.match_vakaros_session(session_id)
    second = await storage.match_vakaros_session(session_id)
    assert first == second == [race_id]


@pytest.mark.asyncio
async def test_rematch_all_vakaros_sessions_links_everything(
    storage: Storage,
) -> None:
    """Historical sessions ingested before the matcher can be linked with one call."""
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    a = _make_vakaros_session(t0, t0 + timedelta(hours=1), source_hash="1" * 64)
    b = _make_vakaros_session(
        t0 + timedelta(hours=2), t0 + timedelta(hours=3), source_hash="2" * 64
    )
    id_a = await storage.store_vakaros_session(a)
    id_b = await storage.store_vakaros_session(b)

    race_a = await _insert_race(
        storage, "Race A", t0 + timedelta(minutes=10), t0 + timedelta(minutes=50)
    )
    race_b = await _insert_race(
        storage,
        "Race B",
        t0 + timedelta(hours=2, minutes=10),
        t0 + timedelta(hours=2, minutes=50),
    )

    results = await storage.rematch_all_vakaros_sessions()
    assert results == {id_a: [race_a], id_b: [race_b]}


@pytest.mark.asyncio
async def test_ingest_vkx_file_auto_links_overlapping_race(
    storage: Storage, tmp_path: object
) -> None:
    """`ingest_vkx_file` should link any overlapping race(s) to the session."""
    import math
    import struct
    from pathlib import Path

    from helmlog.vakaros import ingest_vkx_file

    header = bytes([0xFF, 0x05, 0, 0, 0, 0, 0, 0])
    t0_ms = int(datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    t1_ms = t0_ms + 60_000

    def pos_row(ts_ms: int) -> bytes:
        payload = struct.pack(
            "<Qiifffffff",
            ts_ms,
            round(47.68 / 1e-7),
            round(-122.41 / 1e-7),
            1.0,
            math.radians(0.0),
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
        )
        return bytes([0x02]) + payload

    rows = pos_row(t0_ms) + pos_row(t1_ms)
    terminator = bytes([0xFE]) + struct.pack("<H", len(rows))
    buf = header + rows + terminator

    assert isinstance(tmp_path, Path)
    vkx_path = tmp_path / "auto_match.vkx"
    vkx_path.write_bytes(buf)

    race_id = await _insert_race(
        storage,
        "Race for auto-match",
        datetime(2026, 4, 9, 11, 45, tzinfo=UTC),
        datetime(2026, 4, 9, 12, 30, tzinfo=UTC),
    )

    session_id, was_duplicate = await ingest_vkx_file(storage, vkx_path)
    assert was_duplicate is False
    assert await _race_vakaros_link(storage, race_id) == session_id
