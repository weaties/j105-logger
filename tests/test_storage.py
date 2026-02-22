"""Tests for storage.py — SQLite persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from logger.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_ENVIRONMENTAL,
    PGN_POSITION_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WATER_DEPTH,
    PGN_WIND_DATA,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)

if TYPE_CHECKING:
    from logger.storage import Storage

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigration:
    async def test_migration_creates_tables(self, storage: Storage) -> None:
        """All expected tables exist after migration."""
        db = storage._conn()
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        rows = await cur.fetchall()
        names = {row[0] for row in rows}
        for expected in {
            "schema_version",
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
            "video_sessions",
            "weather",
        }:
            assert expected in names, f"Table {expected!r} not found"

    async def test_migration_version_recorded(self, storage: Storage) -> None:
        from logger.storage import _CURRENT_VERSION

        db = storage._conn()
        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == _CURRENT_VERSION

    async def test_migration_idempotent(self, storage: Storage) -> None:
        """Running migrate() a second time must not error or add duplicate rows."""
        from logger.storage import _MIGRATIONS

        await storage.migrate()
        await storage.migrate()
        db = storage._conn()
        cur = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == len(_MIGRATIONS)  # one row per migration version


# ---------------------------------------------------------------------------
# Write + query round-trips
# ---------------------------------------------------------------------------


class TestWriteQuery:
    async def test_heading_round_trip(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=180.0,
            deviation_deg=None,
            variation_deg=None,
        )
        await storage.write(record)
        rows = await storage.query_range("headings", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["heading_deg"] - 180.0) < 0.001

    async def test_speed_round_trip(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=5.0,
        )
        await storage.write(record)
        rows = await storage.query_range("speeds", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["speed_kts"] - 5.0) < 0.001

    async def test_depth_round_trip(self, storage: Storage) -> None:
        record = DepthRecord(
            pgn=PGN_WATER_DEPTH,
            source_addr=5,
            timestamp=_TS,
            depth_m=10.0,
            offset_m=0.5,
        )
        await storage.write(record)
        rows = await storage.query_range("depths", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["depth_m"] - 10.0) < 0.001
        assert rows[0]["offset_m"] is not None
        assert abs(rows[0]["offset_m"] - 0.5) < 0.001

    async def test_position_round_trip(self, storage: Storage) -> None:
        record = PositionRecord(
            pgn=PGN_POSITION_RAPID,
            source_addr=5,
            timestamp=_TS,
            latitude_deg=37.8044,
            longitude_deg=-122.2712,
        )
        await storage.write(record)
        rows = await storage.query_range("positions", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["latitude_deg"] - 37.8044) < 1e-4
        assert abs(rows[0]["longitude_deg"] - (-122.2712)) < 1e-4

    async def test_cogsog_round_trip(self, storage: Storage) -> None:
        record = COGSOGRecord(
            pgn=PGN_COG_SOG_RAPID,
            source_addr=5,
            timestamp=_TS,
            cog_deg=45.0,
            sog_kts=6.0,
        )
        await storage.write(record)
        rows = await storage.query_range("cogsog", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["cog_deg"] - 45.0) < 0.001
        assert abs(rows[0]["sog_kts"] - 6.0) < 0.001

    async def test_wind_round_trip(self, storage: Storage) -> None:
        record = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=15.0,
            wind_angle_deg=30.0,
            reference=0,
        )
        await storage.write(record)
        rows = await storage.query_range("winds", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["wind_speed_kts"] - 15.0) < 0.001
        assert rows[0]["reference"] == 0

    async def test_environmental_round_trip(self, storage: Storage) -> None:
        record = EnvironmentalRecord(
            pgn=PGN_ENVIRONMENTAL,
            source_addr=5,
            timestamp=_TS,
            water_temp_c=20.0,
        )
        await storage.write(record)
        rows = await storage.query_range("environmental", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["water_temp_c"] - 20.0) < 0.001

    async def test_none_fields_stored_as_null(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=90.0,
            deviation_deg=None,
            variation_deg=None,
        )
        await storage.write(record)
        rows = await storage.query_range("headings", _TS, _TS + timedelta(seconds=1))
        assert rows[0]["deviation_deg"] is None
        assert rows[0]["variation_deg"] is None

    async def test_timestamps_stored_as_utc_iso(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=7.0,
        )
        await storage.write(record)
        rows = await storage.query_range("speeds", _TS, _TS + timedelta(seconds=1))
        ts_str: str = rows[0]["ts"]
        # Must be parseable as ISO 8601 and contain UTC info
        parsed = datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None

    async def test_query_range_excludes_outside(self, storage: Storage) -> None:
        ts_in = _TS + timedelta(seconds=30)
        ts_out = _TS + timedelta(seconds=90)
        for ts, spd in [(ts_in, 5.0), (ts_out, 9.0)]:
            await storage.write(
                SpeedRecord(
                    pgn=PGN_SPEED_THROUGH_WATER,
                    source_addr=5,
                    timestamp=ts,
                    speed_kts=spd,
                )
            )
        rows = await storage.query_range(
            "speeds",
            _TS,
            _TS + timedelta(seconds=60),
        )
        assert len(rows) == 1
        assert abs(rows[0]["speed_kts"] - 5.0) < 0.001

    async def test_query_unknown_table_raises(self, storage: Storage) -> None:
        with pytest.raises(ValueError, match="Unknown table"):
            await storage.query_range(
                "drop_table_injected",
                _TS,
                _TS + timedelta(seconds=1),
            )
