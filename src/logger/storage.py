"""SQLite persistence layer.

Schema is versioned with simple integer migrations. All timestamps are stored
as UTC ISO 8601 strings. The Storage class is the single source of truth for
all logged data.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite
from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

    from logger.audio import AudioSession
    from logger.external import TideReading, WeatherReading
    from logger.races import Race

from logger.nmea2000 import (
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PGNRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)
from logger.video import VideoSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    """Configuration for the SQLite storage backend."""

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/logger.db"))


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

_FLUSH_INTERVAL_S: float = 1.0  # commit to disk at most once per second
_FLUSH_BATCH_SIZE: int = 200  # also flush if this many records are buffered


# ---------------------------------------------------------------------------
# Schema version & migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION: int = 8

_MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS headings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            heading_deg REAL    NOT NULL,
            deviation_deg REAL,
            variation_deg REAL
        );

        CREATE TABLE IF NOT EXISTS speeds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            speed_kts   REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS depths (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            depth_m     REAL    NOT NULL,
            offset_m    REAL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cogsog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            cog_deg     REAL    NOT NULL,
            sog_kts     REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS winds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            source_addr     INTEGER NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_angle_deg  REAL    NOT NULL,
            reference       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS environmental (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            water_temp_c  REAL    NOT NULL
        );
    """,
    2: """
        CREATE INDEX IF NOT EXISTS idx_headings_ts     ON headings(ts);
        CREATE INDEX IF NOT EXISTS idx_speeds_ts       ON speeds(ts);
        CREATE INDEX IF NOT EXISTS idx_depths_ts       ON depths(ts);
        CREATE INDEX IF NOT EXISTS idx_positions_ts    ON positions(ts);
        CREATE INDEX IF NOT EXISTS idx_cogsog_ts       ON cogsog(ts);
        CREATE INDEX IF NOT EXISTS idx_winds_ts        ON winds(ts);
        CREATE INDEX IF NOT EXISTS idx_environmental_ts ON environmental(ts);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS video_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT    NOT NULL,
            video_id      TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            duration_s    REAL    NOT NULL,
            sync_utc      TEXT    NOT NULL,
            sync_offset_s REAL    NOT NULL,
            created_at    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_video_sessions_sync_utc ON video_sessions(sync_utc);
    """,
    4: """
        CREATE TABLE IF NOT EXISTS weather (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            lat             REAL    NOT NULL,
            lon             REAL    NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_dir_deg    REAL    NOT NULL,
            air_temp_c      REAL    NOT NULL,
            pressure_hpa    REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);
    """,
    5: """
        CREATE TABLE IF NOT EXISTS tides (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            station_id   TEXT    NOT NULL,
            station_name TEXT    NOT NULL,
            height_m     REAL    NOT NULL,
            type         TEXT    NOT NULL,
            UNIQUE(ts, station_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tides_ts ON tides(ts);
    """,
    6: """
        CREATE TABLE IF NOT EXISTS audio_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path    TEXT    NOT NULL,
            device_name  TEXT    NOT NULL,
            start_utc    TEXT    NOT NULL,
            end_utc      TEXT,
            sample_rate  INTEGER NOT NULL,
            channels     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audio_sessions_start_utc ON audio_sessions(start_utc);
    """,
    7: """
        CREATE TABLE IF NOT EXISTS races (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            event       TEXT    NOT NULL,
            race_num    INTEGER NOT NULL,
            date        TEXT    NOT NULL,
            start_utc   TEXT    NOT NULL,
            end_utc     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_races_date      ON races(date);
        CREATE INDEX IF NOT EXISTS idx_races_start_utc ON races(start_utc);

        CREATE TABLE IF NOT EXISTS daily_events (
            date        TEXT    PRIMARY KEY,
            event_name  TEXT    NOT NULL
        );
    """,
    8: """
        ALTER TABLE races ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
    """,
}

# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


class Storage:
    """Async SQLite storage for all logger data."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._db: aiosqlite.Connection | None = None
        self._pending: int = 0
        self._last_flush: float = 0.0
        self._session_active: bool = False

    @property
    def session_active(self) -> bool:
        """True when a race or practice session is currently in progress."""
        return self._session_active

    async def connect(self) -> None:
        """Open the database connection."""
        self._db = await aiosqlite.connect(self._config.db_path)
        self._db.row_factory = aiosqlite.Row
        self._last_flush = time.monotonic()
        logger.info("Storage connected: {}", self._config.db_path)
        await self.migrate()
        current = await self.get_current_race()
        self._session_active = current is not None

    async def close(self) -> None:
        """Flush any buffered writes and close the database connection."""
        if self._db is not None:
            await self._flush()
            await self._db.close()
            self._db = None
            logger.info("Storage closed")

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not connected; call connect() first")
        return self._db

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def migrate(self) -> None:
        """Apply any pending schema migrations."""
        db = self._conn()

        # Ensure version table exists
        await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        await db.commit()

        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row and row[0] is not None else 0

        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            logger.info("Applying schema migration v{}", version)
            await db.executescript(_MIGRATIONS[version])
            await db.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )
            await db.commit()

        logger.debug("Schema is at version {}", _CURRENT_VERSION)

    # ------------------------------------------------------------------
    # Write (batched)
    # ------------------------------------------------------------------

    async def write(self, record: PGNRecord) -> None:
        """Buffer a decoded PGN record; flushes to disk periodically."""
        match record:
            case HeadingRecord():
                await self._write_heading(record)
            case SpeedRecord():
                await self._write_speed(record)
            case DepthRecord():
                await self._write_depth(record)
            case PositionRecord():
                await self._write_position(record)
            case COGSOGRecord():
                await self._write_cogsog(record)
            case WindRecord():
                await self._write_wind(record)
            case EnvironmentalRecord():
                await self._write_environmental(record)
        self._pending += 1
        await self._auto_flush()

    async def _auto_flush(self) -> None:
        """Commit if the batch size or time interval threshold is reached."""
        now = time.monotonic()
        if self._pending >= _FLUSH_BATCH_SIZE or now - self._last_flush >= _FLUSH_INTERVAL_S:
            await self._flush()

    async def _flush(self) -> None:
        """Commit all pending writes to disk."""
        if self._pending == 0:
            return
        db = self._conn()
        await db.commit()
        logger.debug("Flushed {} records to SQLite", self._pending)
        self._pending = 0
        self._last_flush = time.monotonic()

    async def _write_heading(self, r: HeadingRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.heading_deg, r.deviation_deg, r.variation_deg),
        )

    async def _write_speed(self, r: SpeedRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.speed_kts),
        )

    async def _write_depth(self, r: DepthRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.depth_m, r.offset_m),
        )

    async def _write_position(self, r: PositionRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.latitude_deg, r.longitude_deg),
        )

    async def _write_cogsog(self, r: COGSOGRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.cog_deg, r.sog_kts),
        )

    async def _write_wind(self, r: WindRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _ts(r.timestamp),
                r.source_addr,
                r.wind_speed_kts,
                r.wind_angle_deg,
                r.reference,
            ),
        )

    async def _write_environmental(self, r: EnvironmentalRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO environmental (ts, source_addr, water_temp_c) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.water_temp_c),
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query_range(
        self,
        table: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return all rows in [start, end] from the given table.

        Timestamps are compared as ISO strings (lexicographic order works for
        UTC ISO 8601 with consistent formatting).
        """
        _ALLOWED_TABLES = {
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
        }
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unknown table: {table!r}")

        db = self._conn()
        cur = await db.execute(
            f"SELECT * FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts",  # noqa: S608
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def status_summary(self) -> dict[str, dict[str, Any]]:
        """Return row counts and last-seen timestamps for each data table."""
        _TABLES = [
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
        ]
        db = self._conn()
        result: dict[str, dict[str, Any]] = {}
        for table in _TABLES:
            cur = await db.execute(f"SELECT COUNT(*), MAX(ts) FROM {table}")  # noqa: S608
            row = await cur.fetchone()
            result[table] = {
                "count": row[0] if row else 0,
                "last_seen": row[1] if (row and row[1]) else "never",
            }
        return result

    # ------------------------------------------------------------------
    # Video sessions
    # ------------------------------------------------------------------

    async def write_video_session(self, session: VideoSession) -> None:
        """Persist a VideoSession to the video_sessions table."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        await db.execute(
            "INSERT INTO video_sessions"
            " (url, video_id, title, duration_s, sync_utc, sync_offset_s, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.url,
                session.video_id,
                session.title,
                session.duration_s,
                session.sync_utc.isoformat(),
                session.sync_offset_s,
                _datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
        logger.info("Video session stored: {!r}", session.title)

    async def list_video_sessions(self) -> list[VideoSession]:
        """Return all stored VideoSessions ordered by sync_utc."""
        from datetime import datetime as _datetime

        db = self._conn()
        cur = await db.execute(
            "SELECT url, video_id, title, duration_s, sync_utc, sync_offset_s"
            " FROM video_sessions ORDER BY sync_utc"
        )
        rows = await cur.fetchall()
        return [
            VideoSession(
                url=row["url"],
                video_id=row["video_id"],
                title=row["title"],
                duration_s=row["duration_s"],
                sync_utc=_datetime.fromisoformat(row["sync_utc"]),
                sync_offset_s=row["sync_offset_s"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------

    async def write_weather(self, reading: WeatherReading) -> None:
        """Persist a WeatherReading to the weather table."""
        db = self._conn()
        await db.execute(
            "INSERT INTO weather"
            " (ts, lat, lon, wind_speed_kts, wind_dir_deg, air_temp_c, pressure_hpa)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _ts(reading.timestamp),
                reading.lat,
                reading.lon,
                reading.wind_speed_kts,
                reading.wind_direction_deg,
                reading.air_temp_c,
                reading.pressure_hpa,
            ),
        )
        await db.commit()
        logger.debug("Weather reading stored: ts={}", _ts(reading.timestamp))

    async def query_weather_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return all weather rows in [start, end] ordered by ts."""
        db = self._conn()
        cur = await db.execute(
            "SELECT * FROM weather WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Tides
    # ------------------------------------------------------------------

    async def write_tide(self, reading: TideReading) -> None:
        """Persist a TideReading to the tides table.

        Uses INSERT OR IGNORE so re-fetching the same predictions is safe.
        """
        db = self._conn()
        await db.execute(
            "INSERT OR IGNORE INTO tides"
            " (ts, station_id, station_name, height_m, type)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _ts(reading.timestamp),
                reading.station_id,
                reading.station_name,
                reading.height_m,
                reading.type,
            ),
        )
        await db.commit()

    async def query_tide_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return all tide rows in [start, end] ordered by ts."""
        db = self._conn()
        cur = await db.execute(
            "SELECT * FROM tides WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def latest_position(self) -> dict[str, Any] | None:
        """Return the most recent row from the positions table, or None."""
        db = self._conn()
        cur = await db.execute("SELECT * FROM positions ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_instruments(self) -> dict[str, float | None]:
        """Return the most recent reading from each instrument table."""
        conn = self._conn()

        async def _q(table: str, cols: str, where: str = "") -> Any:  # noqa: ANN401
            cur = await conn.execute(
                f"SELECT {cols} FROM {table} {where} ORDER BY ts DESC LIMIT 1"  # noqa: S608
            )
            return await cur.fetchone()

        hdg = await _q("headings", "heading_deg")
        spd = await _q("speeds", "speed_kts")
        cs = await _q("cogsog", "cog_deg, sog_kts")
        tw = await _q(
            "winds", "wind_speed_kts, wind_angle_deg, reference", "WHERE reference IN (0, 4)"
        )
        aw = await _q("winds", "wind_speed_kts, wind_angle_deg", "WHERE reference=2")

        heading = hdg["heading_deg"] if hdg else None
        twa: float | None = None
        twd: float | None = None
        tws: float | None = None

        if tw:
            tws = round(tw["wind_speed_kts"], 1)
            tw_ang = tw["wind_angle_deg"]
            if tw["reference"] == 0:
                # Boat-referenced angle (TWA); compute TWD from heading
                twa = round(tw_ang, 1)
                twd = round((heading + twa) % 360, 1) if heading is not None else None
            else:
                # reference=4: north-referenced direction (TWD); compute TWA from heading
                twd = round(tw_ang % 360, 1)
                twa = round((tw_ang - heading + 360) % 360, 1) if heading is not None else None

        return {
            "heading_deg": round(heading, 1) if heading is not None else None,
            "bsp_kts": round(spd["speed_kts"], 2) if spd else None,
            "cog_deg": round(cs["cog_deg"], 1) if cs else None,
            "sog_kts": round(cs["sog_kts"], 2) if cs else None,
            "tws_kts": tws,
            "twa_deg": twa,
            "twd_deg": twd,
            "aws_kts": round(aw["wind_speed_kts"], 1) if aw else None,
            "awa_deg": round(aw["wind_angle_deg"], 1) if aw else None,
        }

    # ------------------------------------------------------------------
    # Audio sessions
    # ------------------------------------------------------------------

    async def write_audio_session(self, session: AudioSession) -> int:
        """Insert an audio session row and return the new row id."""
        from logger.audio import AudioSession as _AudioSession

        assert isinstance(session, _AudioSession)

        db = self._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, end_utc, sample_rate, channels)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.file_path,
                session.device_name,
                session.start_utc.isoformat(),
                session.end_utc.isoformat() if session.end_utc else None,
                session.sample_rate,
                session.channels,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Audio session stored: id={} file={}", cur.lastrowid, session.file_path)
        return cur.lastrowid

    async def update_audio_session_end(self, session_id: int, end_utc: datetime) -> None:
        """Set the end_utc for an existing audio session row."""
        db = self._conn()
        await db.execute(
            "UPDATE audio_sessions SET end_utc = ? WHERE id = ?",
            (end_utc.isoformat(), session_id),
        )
        await db.commit()
        logger.debug("Audio session {} end_utc updated", session_id)

    async def list_audio_sessions(self) -> list[AudioSession]:
        """Return all audio sessions ordered by start_utc descending."""
        from datetime import datetime as _datetime

        from logger.audio import AudioSession as _AudioSession

        db = self._conn()
        cur = await db.execute(
            "SELECT id, file_path, device_name, start_utc, end_utc, sample_rate, channels"
            " FROM audio_sessions ORDER BY start_utc DESC"
        )
        rows = await cur.fetchall()
        return [
            _AudioSession(
                file_path=row["file_path"],
                device_name=row["device_name"],
                start_utc=_datetime.fromisoformat(row["start_utc"]),
                end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                sample_rate=row["sample_rate"],
                channels=row["channels"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Races
    # ------------------------------------------------------------------

    async def start_race(
        self,
        event: str,
        start_utc: datetime,
        date_str: str,
        race_num: int,
        name: str,
        session_type: str = "race",
    ) -> Race:
        """Auto-close any open race for the day, insert a new race row, and return it."""
        from logger.races import Race as _Race

        db = self._conn()

        # Close any open race for this UTC date
        open_cur = await db.execute(
            "SELECT id FROM races WHERE date = ? AND end_utc IS NULL",
            (date_str,),
        )
        open_row = await open_cur.fetchone()
        if open_row is not None:
            await db.execute(
                "UPDATE races SET end_utc = ? WHERE id = ?",
                (start_utc.isoformat(), open_row["id"]),
            )

        cur = await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (name, event, race_num, date_str, start_utc.isoformat(), session_type),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("Race started: {} (id={}) type={}", name, cur.lastrowid, session_type)
        self._session_active = True
        return _Race(
            id=cur.lastrowid,
            name=name,
            event=event,
            race_num=race_num,
            date=date_str,
            start_utc=start_utc,
            end_utc=None,
            session_type=session_type,
        )

    async def end_race(self, race_id: int, end_utc: datetime) -> None:
        """Set end_utc on the given race row."""
        db = self._conn()
        await db.execute(
            "UPDATE races SET end_utc = ? WHERE id = ?",
            (end_utc.isoformat(), race_id),
        )
        await db.commit()
        self._session_active = False
        logger.info("Race {} ended at {}", race_id, end_utc.isoformat())

    async def get_current_race(self) -> Race | None:
        """Return the most recent race with no end_utc, or None."""
        from datetime import datetime as _datetime

        from logger.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races WHERE end_utc IS NULL ORDER BY start_utc DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _Race(
            id=row["id"],
            name=row["name"],
            event=row["event"],
            race_num=row["race_num"],
            date=row["date"],
            start_utc=_datetime.fromisoformat(row["start_utc"]),
            end_utc=None,
            session_type=row["session_type"],
        )

    async def list_races_for_date(self, date_str: str) -> list[Race]:
        """Return all races for a UTC date string, ordered by start_utc ASC."""
        from datetime import datetime as _datetime

        from logger.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races WHERE date = ? ORDER BY start_utc ASC",
            (date_str,),
        )
        rows = await cur.fetchall()
        return [
            _Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_datetime.fromisoformat(row["start_utc"]),
                end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )
            for row in rows
        ]

    async def count_sessions_for_date(self, date_str: str, session_type: str) -> int:
        """Return the count of sessions of the given type for a UTC date string."""
        db = self._conn()
        cur = await db.execute(
            "SELECT COUNT(*) FROM races WHERE date = ? AND session_type = ?",
            (date_str, session_type),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_daily_event(self, date_str: str) -> str | None:
        """Look up a stored custom event name for the given UTC date."""
        db = self._conn()
        cur = await db.execute("SELECT event_name FROM daily_events WHERE date = ?", (date_str,))
        row = await cur.fetchone()
        return row["event_name"] if row else None

    async def set_daily_event(self, date_str: str, event_name: str) -> None:
        """Upsert a custom event name for the given UTC date."""
        db = self._conn()
        await db.execute(
            "INSERT INTO daily_events (date, event_name) VALUES (?, ?)"
            " ON CONFLICT(date) DO UPDATE SET event_name = excluded.event_name",
            (date_str, event_name),
        )
        await db.commit()
        logger.debug("Daily event set: {} → {}", date_str, event_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string."""
    return dt.isoformat()
