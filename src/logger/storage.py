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

_LIVE_KEYS = (
    "heading_deg",
    "bsp_kts",
    "cog_deg",
    "sog_kts",
    "tws_kts",
    "twa_deg",
    "twd_deg",
    "aws_kts",
    "awa_deg",
)


# ---------------------------------------------------------------------------
# Schema version & migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION: int = 10

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
    9: """
        ALTER TABLE audio_sessions ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE audio_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
        ALTER TABLE audio_sessions ADD COLUMN name TEXT;
    """,
    10: """
        CREATE TABLE IF NOT EXISTS race_crew (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            position  TEXT    NOT NULL,
            sailor    TEXT    NOT NULL,
            UNIQUE(race_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_race_crew_race_id ON race_crew(race_id);

        CREATE TABLE IF NOT EXISTS recent_sailors (
            sailor    TEXT PRIMARY KEY,
            last_used TEXT NOT NULL
        );
    """,
}

# Canonical order for the 5 J105 positions
_POSITIONS: tuple[str, ...] = ("helm", "main", "pit", "bow", "tactician")

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
        self._live: dict[str, float | None] = dict.fromkeys(_LIVE_KEYS)
        self._live_tw_ref: int | None = None
        self._live_tw_angle_raw: float | None = None

    @property
    def session_active(self) -> bool:
        """True when a race or practice session is currently in progress."""
        return self._session_active

    # ------------------------------------------------------------------
    # In-memory live instrument cache (always updated, no DB I/O)
    # ------------------------------------------------------------------

    def _recompute_true_wind(self) -> None:
        ref = self._live_tw_ref
        ang = self._live_tw_angle_raw
        hdg = self._live["heading_deg"]
        if ref is None or ang is None:
            return
        if ref == 0:  # boat-referenced angle (TWA)
            self._live["twa_deg"] = round(ang, 1)
            self._live["twd_deg"] = round((hdg + ang) % 360, 1) if hdg is not None else None
        else:  # reference=4: north-referenced direction (TWD)
            self._live["twd_deg"] = round(ang % 360, 1)
            self._live["twa_deg"] = round((ang - hdg + 360) % 360, 1) if hdg is not None else None

    def update_live(self, record: PGNRecord) -> None:
        """Update the in-memory live cache from a decoded record (no DB write)."""
        match record:
            case HeadingRecord():
                self._live["heading_deg"] = round(record.heading_deg, 1)
                self._recompute_true_wind()
            case SpeedRecord():
                self._live["bsp_kts"] = round(record.speed_kts, 2)
            case COGSOGRecord():
                self._live["cog_deg"] = round(record.cog_deg, 1)
                self._live["sog_kts"] = round(record.sog_kts, 2)
            case WindRecord() if record.reference == 2:  # apparent
                self._live["aws_kts"] = round(record.wind_speed_kts, 1)
                self._live["awa_deg"] = round(record.wind_angle_deg, 1)
            case WindRecord() if record.reference in (0, 4):  # true
                self._live["tws_kts"] = round(record.wind_speed_kts, 1)
                self._live_tw_ref = record.reference
                self._live_tw_angle_raw = record.wind_angle_deg
                self._recompute_true_wind()

    def live_instruments(self) -> dict[str, float | None]:
        """Return a snapshot of the current in-memory instrument cache."""
        return dict(self._live)

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
        """Return the most recent reading from each instrument table.

        Prefers the in-memory live cache (updated on every SK/CAN record) so
        the instruments panel stays current even without an active session.
        Falls back to DB queries only on startup before the first message.
        """
        if any(v is not None for v in self._live.values()):
            return self.live_instruments()

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

    async def write_audio_session(
        self,
        session: AudioSession,
        *,
        race_id: int | None = None,
        session_type: str = "race",
        name: str | None = None,
    ) -> int:
        """Insert an audio session row and return the new row id.

        *race_id* links this recording to a race/practice row.
        *session_type* is ``"race"``, ``"practice"``, or ``"debrief"``.
        *name* is a human-readable label (e.g. the race name or debrief name).
        """
        from logger.audio import AudioSession as _AudioSession

        assert isinstance(session, _AudioSession)

        db = self._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, end_utc, sample_rate, channels,"
            "  race_id, session_type, name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.file_path,
                session.device_name,
                session.start_utc.isoformat(),
                session.end_utc.isoformat() if session.end_utc else None,
                session.sample_rate,
                session.channels,
                race_id,
                session_type,
                name,
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

    async def list_sessions(
        self,
        q: str | None = None,
        session_type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return (total_count, sessions) for the history browser.

        Sessions include races and practices (from the ``races`` table) and
        debriefs (from ``audio_sessions`` where ``session_type='debrief'``).
        Results are sorted newest-first.

        *session_type* may be ``"race"``, ``"practice"``, ``"debrief"``, or
        ``None`` for all types.
        """
        db = self._conn()

        include_races = session_type in (None, "race", "practice")
        include_debriefs = session_type in (None, "debrief")

        parts: list[str] = []
        params: list[Any] = []

        if include_races:
            race_where: list[str] = []
            race_params: list[Any] = []
            if session_type in ("race", "practice"):
                race_where.append("r.session_type = ?")
                race_params.append(session_type)
            else:
                race_where.append("r.session_type IN ('race', 'practice')")
            if q:
                race_where.append("(r.name LIKE ? OR r.event LIKE ?)")
                like = f"%{q}%"
                race_params.extend([like, like])
            if from_date:
                race_where.append("r.date >= ?")
                race_params.append(from_date)
            if to_date:
                race_where.append("r.date <= ?")
                race_params.append(to_date)
            where = "WHERE " + " AND ".join(race_where)
            parts.append(
                f"SELECT r.id AS id, r.session_type AS type, r.name AS name,"
                f" r.event AS event, r.race_num AS race_num, r.date AS date,"
                f" r.start_utc AS start_utc, r.end_utc AS end_utc,"
                f" CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END AS has_audio,"
                f" a.id AS audio_session_id,"
                f" NULL AS parent_race_id, NULL AS parent_race_name"
                f" FROM races r"
                f" LEFT JOIN audio_sessions a"
                f"   ON a.race_id = r.id AND a.session_type IN ('race', 'practice')"
                f" {where}"
            )
            params.extend(race_params)

        if include_debriefs:
            deb_where: list[str] = ["a.session_type = 'debrief'"]
            deb_params: list[Any] = []
            if q:
                deb_where.append("(a.name LIKE ? OR r.event LIKE ?)")
                like = f"%{q}%"
                deb_params.extend([like, like])
            if from_date:
                deb_where.append("substr(a.start_utc, 1, 10) >= ?")
                deb_params.append(from_date)
            if to_date:
                deb_where.append("substr(a.start_utc, 1, 10) <= ?")
                deb_params.append(to_date)
            where = "WHERE " + " AND ".join(deb_where)
            parts.append(
                f"SELECT a.id AS id, 'debrief' AS type,"
                f" COALESCE(a.name, a.file_path) AS name,"
                f" COALESCE(r.event, '') AS event,"
                f" r.race_num AS race_num,"
                f" COALESCE(r.date, substr(a.start_utc, 1, 10)) AS date,"
                f" a.start_utc AS start_utc, a.end_utc AS end_utc,"
                f" 1 AS has_audio, a.id AS audio_session_id,"
                f" r.id AS parent_race_id, r.name AS parent_race_name"
                f" FROM audio_sessions a"
                f" LEFT JOIN races r ON r.id = a.race_id"
                f" {where}"
            )
            params.extend(deb_params)

        if not parts:
            return (0, [])

        union = " UNION ALL ".join(parts)

        count_cur = await db.execute(f"SELECT COUNT(*) FROM ({union})", params)
        count_row = await count_cur.fetchone()
        total = int(count_row[0]) if count_row else 0

        data_cur = await db.execute(
            f"SELECT * FROM ({union}) ORDER BY start_utc DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await data_cur.fetchall()

        from datetime import datetime as _datetime

        result: list[dict[str, Any]] = []
        for row in rows:
            start_utc = _datetime.fromisoformat(row["start_utc"])
            end_utc = _datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None
            duration_s = (end_utc - start_utc).total_seconds() if end_utc else None
            result.append(
                {
                    "id": row["id"],
                    "type": row["type"],
                    "name": row["name"],
                    "event": row["event"],
                    "race_num": row["race_num"],
                    "date": row["date"],
                    "start_utc": start_utc.isoformat(),
                    "end_utc": end_utc.isoformat() if end_utc else None,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                    "has_audio": bool(row["has_audio"]),
                    "audio_session_id": row["audio_session_id"],
                    "parent_race_id": row["parent_race_id"],
                    "parent_race_name": row["parent_race_name"],
                    "crew": [],
                }
            )

        # Attach crew to race/practice sessions (one query per session)
        for session in result:
            if session["type"] in ("race", "practice"):
                session["crew"] = await self.get_race_crew(session["id"])

        return (total, result)

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
    # Race crew
    # ------------------------------------------------------------------

    async def set_race_crew(self, race_id: int, crew: list[dict[str, str]]) -> None:
        """Set crew positions for a race (full-replace semantics).

        Each entry must have ``position`` and ``sailor`` keys.  Blank sailor
        names must be filtered by the caller before invoking this method.
        All existing positions not present in *crew* are deleted.
        Each sailor name is upserted into ``recent_sailors``.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()

        if crew:
            positions = [c["position"] for c in crew]
            placeholders = ",".join("?" * len(positions))
            await db.execute(
                f"DELETE FROM race_crew WHERE race_id = ? AND position NOT IN ({placeholders})",
                (race_id, *positions),
            )
        else:
            await db.execute("DELETE FROM race_crew WHERE race_id = ?", (race_id,))

        for entry in crew:
            position = entry["position"]
            sailor = entry["sailor"]
            await db.execute(
                "INSERT OR REPLACE INTO race_crew (race_id, position, sailor) VALUES (?, ?, ?)",
                (race_id, position, sailor),
            )
            await db.execute(
                "INSERT INTO recent_sailors (sailor, last_used) VALUES (?, ?)"
                " ON CONFLICT(sailor) DO UPDATE SET last_used = excluded.last_used",
                (sailor, now_str),
            )

        await db.commit()
        logger.debug("Crew set for race {}: {} positions", race_id, len(crew))

    async def get_race_crew(self, race_id: int) -> list[dict[str, str]]:
        """Return crew for *race_id* ordered by canonical position (helm first)."""
        db = self._conn()
        cur = await db.execute(
            "SELECT position, sailor FROM race_crew WHERE race_id = ?",
            (race_id,),
        )
        rows = await cur.fetchall()
        position_order = {p: i for i, p in enumerate(_POSITIONS)}
        result = [{"position": row["position"], "sailor": row["sailor"]} for row in rows]
        result.sort(key=lambda x: position_order.get(x["position"], 99))
        return result

    async def get_recent_sailors(self, limit: int = 10) -> list[str]:
        """Return the most recently used sailor names, newest first."""
        db = self._conn()
        cur = await db.execute(
            "SELECT sailor FROM recent_sailors ORDER BY last_used DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [row["sailor"] for row in rows]

    async def get_last_session_crew(self) -> list[dict[str, str]]:
        """Return crew from the most recently ended race/practice, or [] if none."""
        db = self._conn()
        cur = await db.execute(
            "SELECT position, sailor FROM race_crew"
            " WHERE race_id = ("
            "   SELECT id FROM races WHERE end_utc IS NOT NULL"
            "   ORDER BY end_utc DESC LIMIT 1"
            " )"
        )
        rows = await cur.fetchall()
        position_order = {p: i for i, p in enumerate(_POSITIONS)}
        result = [{"position": row["position"], "sailor": row["sailor"]} for row in rows]
        result.sort(key=lambda x: position_order.get(x["position"], 99))
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string."""
    return dt.isoformat()
