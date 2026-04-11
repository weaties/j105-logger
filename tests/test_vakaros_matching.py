"""Tests for Vakaros -> races session matching (#458).

Rule (from the spec): a Vakaros session matches an SK race when their
time windows overlap by at least 50% of the shorter session's duration.
Races still in progress (end_utc IS NULL) are never matched.
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
    """Raw-SQL insert a race for testing (bypasses slug/validation)."""
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
async def test_match_returns_none_when_no_races_exist(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    matched = await storage.match_vakaros_session_to_race(session_id)
    assert matched is None

    db = storage._conn()
    cur = await db.execute(
        "SELECT matched_race_id FROM vakaros_sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    assert row["matched_race_id"] is None


@pytest.mark.asyncio
async def test_match_links_race_that_is_fully_inside_vakaros_window(
    storage: Storage,
) -> None:
    # Vakaros session: 12:00 - 14:00 (2 hours)
    # Race          : 12:30 - 13:30 (1 hour, fully inside)
    # Shorter duration = race (60 min). Overlap = 60 min. Ratio = 1.0 >= 0.5.
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(
        storage,
        "Race 1",
        t0 + timedelta(minutes=30),
        t0 + timedelta(minutes=90),
    )

    matched = await storage.match_vakaros_session_to_race(session_id)
    assert matched == race_id

    db = storage._conn()
    cur = await db.execute(
        "SELECT matched_race_id FROM vakaros_sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    assert row["matched_race_id"] == race_id


@pytest.mark.asyncio
async def test_match_rejects_race_with_less_than_50_percent_overlap(
    storage: Storage,
) -> None:
    # Vakaros: 12:00 - 13:00 (60 min)
    # Race   : 12:50 - 13:50 (60 min, overlaps only 10 min = 16.7%)
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=1))
    session_id = await storage.store_vakaros_session(session)

    await _insert_race(
        storage,
        "Race far",
        t0 + timedelta(minutes=50),
        t0 + timedelta(minutes=110),
    )

    matched = await storage.match_vakaros_session_to_race(session_id)
    assert matched is None


@pytest.mark.asyncio
async def test_match_ignores_race_with_null_end_utc(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    await _insert_race(storage, "In-progress race", t0 + timedelta(minutes=10), None)

    matched = await storage.match_vakaros_session_to_race(session_id)
    assert matched is None


@pytest.mark.asyncio
async def test_match_picks_race_with_highest_overlap_ratio(storage: Storage) -> None:
    # Vakaros: 12:00 - 13:00 (60 min)
    # Race A : 12:00 - 12:40 (40 min, overlap 40 min, ratio = 40/40 = 1.0)
    # Race B : 12:30 - 13:30 (60 min, overlap 30 min, ratio = 30/60 = 0.5)
    # Race A wins.
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=1))
    session_id = await storage.store_vakaros_session(session)

    race_a = await _insert_race(storage, "Race A", t0, t0 + timedelta(minutes=40))
    await _insert_race(
        storage,
        "Race B",
        t0 + timedelta(minutes=30),
        t0 + timedelta(minutes=90),
    )

    matched = await storage.match_vakaros_session_to_race(session_id)
    assert matched == race_a


@pytest.mark.asyncio
async def test_ingest_vkx_file_auto_matches_to_race(storage: Storage, tmp_path: object) -> None:
    """`ingest_vkx_file` should link a session to any overlapping race."""
    import math
    import struct
    from pathlib import Path

    from helmlog.vakaros import ingest_vkx_file

    # Build a 60-second VKX with two position rows, on a known race day.
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

    # Race that fully contains the Vakaros 60-second window.
    race_id = await _insert_race(
        storage,
        "Race for auto-match",
        datetime(2026, 4, 9, 11, 45, tzinfo=UTC),
        datetime(2026, 4, 9, 12, 30, tzinfo=UTC),
    )

    session_id, was_duplicate = await ingest_vkx_file(storage, vkx_path)
    assert was_duplicate is False

    db = storage._conn()
    cur = await db.execute(
        "SELECT matched_race_id FROM vakaros_sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    assert row["matched_race_id"] == race_id


@pytest.mark.asyncio
async def test_match_is_idempotent_and_updates_link(storage: Storage) -> None:
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = _make_vakaros_session(t0, t0 + timedelta(hours=2))
    session_id = await storage.store_vakaros_session(session)

    race_id = await _insert_race(
        storage, "Race", t0 + timedelta(minutes=30), t0 + timedelta(minutes=90)
    )

    # Running twice should yield the same link.
    first = await storage.match_vakaros_session_to_race(session_id)
    second = await storage.match_vakaros_session_to_race(session_id)
    assert first == second == race_id
