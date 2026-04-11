"""Tests for Vakaros session storage (schema v59)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.vakaros import (
    LinePosition,
    LinePositionType,
    PositionRow,
    RaceTimerEvent,
    RaceTimerEventType,
    VakarosSession,
    WindRow,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _make_session(source_hash: str = "a" * 64, source_file: str = "test.vkx") -> VakarosSession:
    ts0 = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    ts1 = datetime(2024, 6, 15, 12, 0, 1, tzinfo=UTC)
    ts2 = datetime(2024, 6, 15, 12, 0, 2, tzinfo=UTC)
    return VakarosSession(
        source_hash=source_hash,
        source_file=source_file,
        start_utc=ts0,
        end_utc=ts2,
        positions=(
            PositionRow(
                timestamp=ts0,
                latitude_deg=37.8044,
                longitude_deg=-122.2712,
                sog_mps=3.5,
                cog_deg=45.0,
                altitude_m=10.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=ts1,
                latitude_deg=37.8045,
                longitude_deg=-122.2711,
                sog_mps=3.6,
                cog_deg=46.0,
                altitude_m=10.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=ts2,
                latitude_deg=37.8046,
                longitude_deg=-122.2710,
                sog_mps=3.7,
                cog_deg=47.0,
                altitude_m=10.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            LinePosition(
                timestamp=ts0,
                line_type=LinePositionType.PIN,
                latitude_deg=37.8050,
                longitude_deg=-122.2700,
            ),
            LinePosition(
                timestamp=ts0,
                line_type=LinePositionType.BOAT,
                latitude_deg=37.8048,
                longitude_deg=-122.2705,
            ),
        ),
        race_events=(
            RaceTimerEvent(
                timestamp=ts1,
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
        ),
        winds=(WindRow(timestamp=ts1, direction_deg=215.0, speed_mps=7.5),),
    )


@pytest.mark.asyncio
async def test_schema_v59_creates_vakaros_tables(storage: Storage) -> None:
    db = storage._conn()  # test-only access
    for table in (
        "vakaros_sessions",
        "vakaros_positions",
        "vakaros_line_positions",
        "vakaros_race_events",
        "vakaros_winds",
    ):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        row = await cur.fetchone()
        assert row is not None, f"missing table {table}"


@pytest.mark.asyncio
async def test_store_vakaros_session_inserts_all_rows(storage: Storage) -> None:
    session = _make_session()
    session_id = await storage.store_vakaros_session(session)
    assert session_id > 0

    db = storage._conn()

    cur = await db.execute(
        "SELECT source_hash, source_file, start_utc, end_utc FROM vakaros_sessions WHERE id=?",
        (session_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["source_hash"] == session.source_hash
    assert row["source_file"] == "test.vkx"

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_positions WHERE session_id=?",
        (session_id,),
    )
    assert (await cur.fetchone())["n"] == 3

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_line_positions WHERE session_id=?",
        (session_id,),
    )
    assert (await cur.fetchone())["n"] == 2

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_race_events WHERE session_id=?",
        (session_id,),
    )
    assert (await cur.fetchone())["n"] == 1

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_winds WHERE session_id=?",
        (session_id,),
    )
    assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_store_vakaros_session_is_idempotent_on_source_hash(storage: Storage) -> None:
    session = _make_session(source_hash="b" * 64)
    first_id = await storage.store_vakaros_session(session)
    second_id = await storage.store_vakaros_session(session)
    assert first_id == second_id

    db = storage._conn()
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_sessions WHERE source_hash=?",
        (session.source_hash,),
    )
    assert (await cur.fetchone())["n"] == 1

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM vakaros_positions WHERE session_id=?",
        (first_id,),
    )
    assert (await cur.fetchone())["n"] == 3  # not doubled


@pytest.mark.asyncio
async def test_delete_vakaros_session_cascades(storage: Storage) -> None:
    session = _make_session(source_hash="c" * 64)
    session_id = await storage.store_vakaros_session(session)

    await storage.delete_vakaros_session(session_id)

    db = storage._conn()
    for table in (
        "vakaros_sessions",
        "vakaros_positions",
        "vakaros_line_positions",
        "vakaros_race_events",
        "vakaros_winds",
    ):
        cur = await db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE "
            f"{'id' if table == 'vakaros_sessions' else 'session_id'}=?",
            (session_id,),
        )
        assert (await cur.fetchone())["n"] == 0, f"{table} not cleared"
