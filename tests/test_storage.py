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
            "tides",
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


# ---------------------------------------------------------------------------
# Session gate flag tests
# ---------------------------------------------------------------------------

_DATE = "2025-08-10"
_START1 = datetime(2025, 8, 10, 13, 45, 0, tzinfo=UTC)
_START2 = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)
_END1 = datetime(2025, 8, 10, 14, 5, 0, tzinfo=UTC)


class TestSessionGate:
    async def test_session_active_false_initially(self, storage: Storage) -> None:
        assert storage.session_active is False

    async def test_session_active_true_after_start_race(self, storage: Storage) -> None:
        await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
        assert storage.session_active is True

    async def test_session_active_false_after_end_race(self, storage: Storage) -> None:
        race = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
        await storage.end_race(race.id, _END1)
        assert storage.session_active is False

    async def test_count_sessions_for_date(self, storage: Storage) -> None:
        await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1", "race")
        await storage.start_race(
            "BallardCup", _START2, _DATE, 1, "20250810-BallardCup-P1", "practice"
        )
        assert await storage.count_sessions_for_date(_DATE, "race") == 1
        assert await storage.count_sessions_for_date(_DATE, "practice") == 1
        assert await storage.count_sessions_for_date("2025-08-11", "race") == 0


# ---------------------------------------------------------------------------
# Live instrument cache tests
# ---------------------------------------------------------------------------


class TestLiveCache:
    def test_cache_initially_empty(self, storage: Storage) -> None:
        result = storage.live_instruments()
        assert all(v is None for v in result.values())

    def test_update_live_heading(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=270.0,
            deviation_deg=None,
            variation_deg=None,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["heading_deg"] == 270.0

    def test_update_live_speed(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=6.54,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["bsp_kts"] == 6.54

    def test_update_live_cogsog(self, storage: Storage) -> None:
        record = COGSOGRecord(
            pgn=PGN_COG_SOG_RAPID,
            source_addr=5,
            timestamp=_TS,
            cog_deg=45.0,
            sog_kts=7.0,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["cog_deg"] == 45.0
        assert result["sog_kts"] == 7.0

    def test_update_live_apparent_wind(self, storage: Storage) -> None:
        record = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=12.0,
            wind_angle_deg=35.0,
            reference=2,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["aws_kts"] == 12.0
        assert result["awa_deg"] == 35.0

    def test_update_live_true_wind_boat_ref(self, storage: Storage) -> None:
        """reference=0 (TWA boat-referenced): TWD computed from heading."""
        hdg = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=100.0,
            deviation_deg=None,
            variation_deg=None,
        )
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=45.0,
            reference=0,
        )
        storage.update_live(hdg)
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["tws_kts"] == 10.0
        assert result["twa_deg"] == 45.0
        assert result["twd_deg"] == 145.0  # (100 + 45) % 360

    def test_update_live_true_wind_north_ref(self, storage: Storage) -> None:
        """reference=4 (TWD north-referenced): TWA computed from heading."""
        hdg = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=100.0,
            deviation_deg=None,
            variation_deg=None,
        )
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=145.0,
            reference=4,
        )
        storage.update_live(hdg)
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["tws_kts"] == 10.0
        assert result["twd_deg"] == 145.0
        assert result["twa_deg"] == 45.0  # (145 - 100 + 360) % 360

    def test_update_live_twa_no_heading(self, storage: Storage) -> None:
        """TWD is None when heading unavailable and reference=0."""
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=45.0,
            reference=0,
        )
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["twa_deg"] == 45.0
        assert result["twd_deg"] is None

    async def test_latest_instruments_uses_live_cache(self, storage: Storage) -> None:
        """latest_instruments() returns live values when cache is populated."""
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=8.5,
        )
        storage.update_live(record)
        result = await storage.latest_instruments()
        assert result["bsp_kts"] == 8.5

    async def test_latest_instruments_db_fallback_when_cache_empty(self, storage: Storage) -> None:
        """latest_instruments() falls back to DB when cache is entirely empty."""
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=4.2,
        )
        await storage.write(record)
        result = await storage.latest_instruments()
        assert result["bsp_kts"] is not None
        assert abs(result["bsp_kts"] - 4.2) < 0.01


# ---------------------------------------------------------------------------
# Crew storage tests
# ---------------------------------------------------------------------------

_CREW_DATE = "2025-09-01"
_CREW_START = datetime(2025, 9, 1, 14, 0, 0, tzinfo=UTC)


class TestCrewStorage:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race("Regatta", _CREW_START, _CREW_DATE, 1, "20250901-Regatta-1")
        assert race.id is not None
        return race.id

    async def test_set_and_get_race_crew(self, storage: Storage) -> None:
        """set_race_crew then get_race_crew returns all positions in canonical order."""
        race_id = await self._make_race(storage)
        crew_in = [
            {"position": "tactician", "sailor": "Bill"},
            {"position": "helm", "sailor": "Mark"},
            {"position": "pit", "sailor": "Sarah"},
            {"position": "main", "sailor": "Dave"},
            {"position": "bow", "sailor": "Tom"},
        ]
        await storage.set_race_crew(race_id, crew_in)
        crew_out = await storage.get_race_crew(race_id)
        positions = [c["position"] for c in crew_out]
        assert positions == ["helm", "main", "pit", "bow", "tactician"]
        sailors = {c["position"]: c["sailor"] for c in crew_out}
        assert sailors["helm"] == "Mark"
        assert sailors["tactician"] == "Bill"

    async def test_set_crew_updates_recent_sailors(self, storage: Storage) -> None:
        """After set_race_crew, get_recent_sailors returns all crew names."""
        race_id = await self._make_race(storage)
        await storage.set_race_crew(
            race_id,
            [{"position": "helm", "sailor": "Alice"}, {"position": "main", "sailor": "Bob"}],
        )
        recent = await storage.get_recent_sailors()
        assert "Alice" in recent
        assert "Bob" in recent

    async def test_set_crew_upsert(self, storage: Storage) -> None:
        """Second set_race_crew call wins; old positions removed if absent."""
        race_id = await self._make_race(storage)
        await storage.set_race_crew(
            race_id,
            [{"position": "helm", "sailor": "Mark"}, {"position": "main", "sailor": "Dave"}],
        )
        # Second write: replace helm, drop main, add pit
        await storage.set_race_crew(
            race_id,
            [{"position": "helm", "sailor": "New"}, {"position": "pit", "sailor": "Pat"}],
        )
        crew = await storage.get_race_crew(race_id)
        pos_map = {c["position"]: c["sailor"] for c in crew}
        assert pos_map.get("helm") == "New"
        assert pos_map.get("pit") == "Pat"
        assert "main" not in pos_map

    async def test_get_crew_empty_race(self, storage: Storage) -> None:
        """get_race_crew returns empty list for a race with no crew set."""
        race_id = await self._make_race(storage)
        crew = await storage.get_race_crew(race_id)
        assert crew == []

    async def test_get_recent_sailors_ordered_by_recency(self, storage: Storage) -> None:
        """get_recent_sailors returns names newest-first."""
        race_id = await self._make_race(storage)
        await storage.set_race_crew(race_id, [{"position": "helm", "sailor": "Older"}])
        # A second race to set a newer name
        race2 = await storage.start_race(
            "Regatta", _CREW_START, _CREW_DATE, 2, "20250901-Regatta-2"
        )
        await storage.set_race_crew(race2.id, [{"position": "helm", "sailor": "Newer"}])
        recent = await storage.get_recent_sailors()
        assert recent[0] == "Newer"
