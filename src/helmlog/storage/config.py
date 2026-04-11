"""Storage configuration and constants."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StorageConfig:
    """Configuration for the SQLite storage backend."""

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/logger.db"))
    rudder_storage_hz: float = field(
        default_factory=lambda: float(os.environ.get("RUDDER_STORAGE_HZ", "2"))
    )


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
    "rudder_deg",
)

RACE_SLUG_RETENTION_DAYS: int = 30
_SAIL_TYPES: tuple[str, ...] = ("main", "jib", "spinnaker")

_MARK_REFERENCES: frozenset[str] = frozenset(
    {
        "start",
        "finish",
        *(f"weather_mark_{i}" for i in range(1, 10)),
        *(f"leeward_mark_{i}" for i in range(1, 10)),
        *(f"gate_{i}" for i in range(1, 10)),
        *(f"offset_mark_{i}" for i in range(1, 10)),
    }
)

_CURRENT_VERSION: int = 60

# For tests
_ts: str = "2026-04-11T12:00:00+00:00"
