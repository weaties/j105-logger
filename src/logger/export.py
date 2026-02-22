"""Export logged data to CSV.

Joins all tables by timestamp (one row per second) using standard sailing
column names: BSP, TWS, TWA, AWA, AWS, HDG, COG, SOG, LAT, LON, DEPTH, WTEMP.

Missing data for a given second produces NULL/empty cells (not errors).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportConfig:
    """Configuration for CSV export."""

    output_path: str = field(default_factory=lambda: "data/export.csv")


# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Standard sailing column names used in output CSV
_COLUMNS = [
    "timestamp",
    "HDG",       # heading (degrees true)
    "BSP",       # boatspeed through water (knots)
    "DEPTH",     # water depth (metres)
    "LAT",       # latitude (degrees)
    "LON",       # longitude (degrees)
    "COG",       # course over ground (degrees true)
    "SOG",       # speed over ground (knots)
    "TWS",       # true wind speed (knots) — reference=0 in PGN 130306
    "TWA",       # true wind angle (degrees) — reference=0
    "AWA",       # apparent wind angle (degrees) — reference=2
    "AWS",       # apparent wind speed (knots) — reference=2
    "WTEMP",     # water temperature (Celsius)
    "video_url", # YouTube deep-link for this second (empty if no video linked)
]

# Wind reference codes from PGN 130306
_WIND_REF_TRUE = 0
_WIND_REF_APPARENT = 2


# ---------------------------------------------------------------------------
# Export function
# ---------------------------------------------------------------------------


async def export_csv(
    storage: Storage,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> int:
    """Export all data in [start, end] to a CSV file.

    Iterates second-by-second over the range and picks the most recent reading
    from each table that falls within that second. Missing data is written as
    an empty string.

    Args:
        storage:     Connected Storage instance to read from.
        start:       Start of export range (UTC, inclusive).
        end:         End of export range (UTC, inclusive).
        output_path: Destination CSV file path.

    Returns:
        The number of data rows written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load all tables at once to avoid per-second queries
    logger.info("Loading data for export: {} → {}", start.isoformat(), end.isoformat())

    video_sessions = await storage.list_video_sessions()
    headings = await storage.query_range("headings", start, end)
    speeds = await storage.query_range("speeds", start, end)
    depths = await storage.query_range("depths", start, end)
    positions = await storage.query_range("positions", start, end)
    cogsog = await storage.query_range("cogsog", start, end)
    winds = await storage.query_range("winds", start, end)
    environmental = await storage.query_range("environmental", start, end)

    # Build per-second lookup indexes (ts → last value within that second)
    hdg_idx = _index_by_second(headings)
    bsp_idx = _index_by_second(speeds)
    dep_idx = _index_by_second(depths)
    pos_idx = _index_by_second(positions)
    cs_idx = _index_by_second(cogsog)
    # Split winds by reference type
    true_wind_idx = _index_by_second([r for r in winds if r.get("reference") == _WIND_REF_TRUE])
    app_wind_idx = _index_by_second([r for r in winds if r.get("reference") == _WIND_REF_APPARENT])
    env_idx = _index_by_second(environmental)

    rows_written = 0
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()

        current = _floor_second(start)
        while current <= end:
            sec_key = _second_key(current)
            row: dict[str, Any] = {"timestamp": current.isoformat()}

            if (h := hdg_idx.get(sec_key)) is not None:
                row["HDG"] = _fmt(h.get("heading_deg"))
            else:
                row["HDG"] = ""

            if (s := bsp_idx.get(sec_key)) is not None:
                row["BSP"] = _fmt(s.get("speed_kts"))
            else:
                row["BSP"] = ""

            if (d := dep_idx.get(sec_key)) is not None:
                row["DEPTH"] = _fmt(d.get("depth_m"))
            else:
                row["DEPTH"] = ""

            if (p := pos_idx.get(sec_key)) is not None:
                row["LAT"] = _fmt(p.get("latitude_deg"))
                row["LON"] = _fmt(p.get("longitude_deg"))
            else:
                row["LAT"] = ""
                row["LON"] = ""

            if (cs := cs_idx.get(sec_key)) is not None:
                row["COG"] = _fmt(cs.get("cog_deg"))
                row["SOG"] = _fmt(cs.get("sog_kts"))
            else:
                row["COG"] = ""
                row["SOG"] = ""

            if (tw := true_wind_idx.get(sec_key)) is not None:
                row["TWS"] = _fmt(tw.get("wind_speed_kts"))
                row["TWA"] = _fmt(tw.get("wind_angle_deg"))
            else:
                row["TWS"] = ""
                row["TWA"] = ""

            if (aw := app_wind_idx.get(sec_key)) is not None:
                row["AWA"] = _fmt(aw.get("wind_angle_deg"))
                row["AWS"] = _fmt(aw.get("wind_speed_kts"))
            else:
                row["AWA"] = ""
                row["AWS"] = ""

            if (e := env_idx.get(sec_key)) is not None:
                row["WTEMP"] = _fmt(e.get("water_temp_c"))
            else:
                row["WTEMP"] = ""

            # YouTube deep-link: use the first session that covers this second
            row["video_url"] = ""
            for session in video_sessions:
                link = session.url_at(current)
                if link is not None:
                    row["video_url"] = link
                    break

            writer.writerow(row)
            rows_written += 1
            current += timedelta(seconds=1)

    logger.info("Export complete: {} rows written to {}", rows_written, output_path)
    return rows_written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _floor_second(dt: datetime) -> datetime:
    """Truncate a datetime to the nearest second."""
    return dt.replace(microsecond=0, tzinfo=dt.tzinfo or UTC)


def _second_key(dt: datetime) -> str:
    """Return a string key for the second bucket (no timezone suffix)."""
    return dt.isoformat()[:19]  # "YYYY-MM-DDTHH:MM:SS"


def _index_by_second(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a dict mapping second-keys to the last row within that second.

    Later rows (higher id) within the same second overwrite earlier ones,
    giving us the most recent reading per second.
    """
    idx: dict[str, dict[str, Any]] = {}
    for row in rows:
        ts_str: str = row["ts"]
        # Truncate to second for the bucket key
        bucket = ts_str[:19]  # "YYYY-MM-DDTHH:MM:SS"
        idx[bucket] = row
    return idx


def _fmt(value: object) -> str:
    """Format a numeric value for CSV output, or empty string for None."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)
