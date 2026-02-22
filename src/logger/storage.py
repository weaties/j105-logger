"""SQLite persistence layer.

Schema is versioned with simple integer migrations. All timestamps are stored
as UTC ISO 8601 strings. The Storage class is the single source of truth for
all logged data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite
from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    """Configuration for the SQLite storage backend."""

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/logger.db"))


# ---------------------------------------------------------------------------
# Schema version & migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION: int = 1

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
}

# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


class Storage:
    """Async SQLite storage for all logger data."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database connection."""
        self._db = await aiosqlite.connect(self._config.db_path)
        self._db.row_factory = aiosqlite.Row
        logger.info("Storage connected: {}", self._config.db_path)
        await self.migrate()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
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
    # Write
    # ------------------------------------------------------------------

    async def write(self, record: PGNRecord) -> None:
        """Persist a decoded PGN record to the appropriate table."""
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

    async def _write_heading(self, r: HeadingRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.heading_deg, r.deviation_deg, r.variation_deg),
        )
        await db.commit()

    async def _write_speed(self, r: SpeedRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.speed_kts),
        )
        await db.commit()

    async def _write_depth(self, r: DepthRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.depth_m, r.offset_m),
        )
        await db.commit()

    async def _write_position(self, r: PositionRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.latitude_deg, r.longitude_deg),
        )
        await db.commit()

    async def _write_cogsog(self, r: COGSOGRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.cog_deg, r.sog_kts),
        )
        await db.commit()

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
        await db.commit()

    async def _write_environmental(self, r: EnvironmentalRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO environmental (ts, source_addr, water_temp_c) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.water_temp_c),
        )
        await db.commit()

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string."""
    return dt.isoformat()
