"""Export logged data to CSV, GPX, or JSON.

Joins all tables by timestamp (one row per second) using standard sailing
column names. Missing data for a given second produces null/empty values,
not errors.

Supported output formats (auto-detected from the file extension):
  .csv   — one row per second, empty string for missing values
  .gpx   — GPX 1.1 track; only seconds with position data produce <trkpt>s
  .json  — structured JSON with typed numeric values (null for missing)
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Column / field definitions
# ---------------------------------------------------------------------------

# Ordered list of all output fields (also the CSV column order)
_COLUMNS = [
    "timestamp",
    "HDG",  # heading (degrees true)
    "BSP",  # boatspeed through water (knots)
    "DEPTH",  # water depth (metres)
    "LAT",  # latitude (degrees)
    "LON",  # longitude (degrees)
    "COG",  # course over ground (degrees true)
    "SOG",  # speed over ground (knots)
    "TWS",  # true wind speed (knots)
    "TWA",  # true wind angle (degrees)
    "AWA",  # apparent wind angle (degrees)
    "AWS",  # apparent wind speed (knots)
    "WTEMP",  # water temperature (Celsius)
    "video_url",
    "WX_TWS",  # synoptic wind speed (knots) — Open-Meteo
    "WX_TWD",  # synoptic wind direction (°) — Open-Meteo
    "AIR_TEMP",  # air temperature (°C) — Open-Meteo
    "PRESSURE",  # surface pressure (hPa) — Open-Meteo
    "TIDE_HT",  # tide height above MLLW (metres) — NOAA CO-OPS
]

_WIND_REF_TRUE = 0
_WIND_REF_APPARENT = 2

# Sailing extension namespace used in GPX <extensions>
_GPX_NS = "http://www.topografix.com/GPX/1/1"
_SAIL_NS = "http://github.com/weaties/j105-logger"

# ---------------------------------------------------------------------------
# Internal: shared data loading
# ---------------------------------------------------------------------------


@dataclass
class _Indexes:
    """All per-second and per-hour lookup tables for one export range."""

    video_sessions: list[Any]
    hdg: dict[str, dict[str, Any]]
    bsp: dict[str, dict[str, Any]]
    dep: dict[str, dict[str, Any]]
    pos: dict[str, dict[str, Any]]
    cs: dict[str, dict[str, Any]]
    tw: dict[str, dict[str, Any]]
    aw: dict[str, dict[str, Any]]
    env: dict[str, dict[str, Any]]
    wx: dict[str, dict[str, Any]]
    tide: dict[str, dict[str, Any]]


async def _load(storage: Storage, start: datetime, end: datetime) -> _Indexes:
    """Fetch all tables and build lookup indexes for the export range."""
    logger.info("Loading data for export: {} → {}", start.isoformat(), end.isoformat())

    video_sessions = await storage.list_video_sessions()
    weather_rows = await storage.query_weather_range(start, end)
    tide_rows = await storage.query_tide_range(start, end)
    headings = await storage.query_range("headings", start, end)
    speeds = await storage.query_range("speeds", start, end)
    depths = await storage.query_range("depths", start, end)
    positions = await storage.query_range("positions", start, end)
    cogsog = await storage.query_range("cogsog", start, end)
    winds = await storage.query_range("winds", start, end)
    environmental = await storage.query_range("environmental", start, end)

    return _Indexes(
        video_sessions=video_sessions,
        hdg=_by_second(headings),
        bsp=_by_second(speeds),
        dep=_by_second(depths),
        pos=_by_second(positions),
        cs=_by_second(cogsog),
        tw=_by_second([r for r in winds if r.get("reference") == _WIND_REF_TRUE]),
        aw=_by_second([r for r in winds if r.get("reference") == _WIND_REF_APPARENT]),
        env=_by_second(environmental),
        wx=_by_hour(weather_rows),
        tide=_by_hour(tide_rows),
    )


def _build_row(current: datetime, idx: _Indexes) -> dict[str, float | str | None]:
    """Build one second's worth of data.

    Numeric fields are float | None (None = no reading for that second).
    timestamp and video_url are str | None.
    """
    sk = _second_key(current)
    hk = _hour_key(current)

    row: dict[str, float | str | None] = {"timestamp": current.isoformat()}

    h = idx.hdg.get(sk)
    row["HDG"] = _flt(h, "heading_deg") if h else None

    s = idx.bsp.get(sk)
    row["BSP"] = _flt(s, "speed_kts") if s else None

    d = idx.dep.get(sk)
    row["DEPTH"] = _flt(d, "depth_m") if d else None

    p = idx.pos.get(sk)
    row["LAT"] = _flt(p, "latitude_deg") if p else None
    row["LON"] = _flt(p, "longitude_deg") if p else None

    cs = idx.cs.get(sk)
    row["COG"] = _flt(cs, "cog_deg") if cs else None
    row["SOG"] = _flt(cs, "sog_kts") if cs else None

    tw = idx.tw.get(sk)
    row["TWS"] = _flt(tw, "wind_speed_kts") if tw else None
    row["TWA"] = _flt(tw, "wind_angle_deg") if tw else None

    aw = idx.aw.get(sk)
    row["AWA"] = _flt(aw, "wind_angle_deg") if aw else None
    row["AWS"] = _flt(aw, "wind_speed_kts") if aw else None

    e = idx.env.get(sk)
    row["WTEMP"] = _flt(e, "water_temp_c") if e else None

    row["video_url"] = None
    for session in idx.video_sessions:
        link = session.url_at(current)
        if link is not None:
            row["video_url"] = link
            break

    wx = idx.wx.get(hk)
    row["WX_TWS"] = _flt(wx, "wind_speed_kts") if wx else None
    row["WX_TWD"] = _flt(wx, "wind_dir_deg") if wx else None
    row["AIR_TEMP"] = _flt(wx, "air_temp_c") if wx else None
    row["PRESSURE"] = _flt(wx, "pressure_hpa") if wx else None

    tide = idx.tide.get(hk)
    row["TIDE_HT"] = _flt(tide, "height_m") if tide else None

    return row


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


async def export_csv(
    storage: Storage,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> int:
    """Export all data in [start, end] to a CSV file.

    One row per second; missing data is written as an empty string.

    Returns:
        Number of rows written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    idx = await _load(storage, start, end)
    rows_written = 0

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        current = _floor_second(start)
        while current <= end:
            row = _build_row(current, idx)
            writer.writerow({k: _fmt(v) for k, v in row.items()})
            rows_written += 1
            current += timedelta(seconds=1)

    logger.info("CSV export complete: {} rows → {}", rows_written, output_path)
    return rows_written


# ---------------------------------------------------------------------------
# GPX export
# ---------------------------------------------------------------------------


async def export_gpx(
    storage: Storage,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> int:
    """Export data to a GPX 1.1 track file.

    Only seconds with a valid GPS position produce a <trkpt>. Sailing data
    (HDG, BSP, wind, etc.) is written in a <sail:…> extension block.

    Returns:
        Total seconds processed (same window as CSV for consistency).
    """
    import xml.etree.ElementTree as ET

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    idx = await _load(storage, start, end)

    ET.register_namespace("", _GPX_NS)
    ET.register_namespace("sail", _SAIL_NS)

    gpx = ET.Element(
        f"{{{_GPX_NS}}}gpx",
        {"version": "1.1", "creator": "j105-logger"},
    )
    meta = ET.SubElement(gpx, f"{{{_GPX_NS}}}metadata")
    ET.SubElement(meta, f"{{{_GPX_NS}}}time").text = start.isoformat()

    trk = ET.SubElement(gpx, f"{{{_GPX_NS}}}trk")
    ET.SubElement(trk, f"{{{_GPX_NS}}}name").text = f"J105 {start.date()}"
    trkseg = ET.SubElement(trk, f"{{{_GPX_NS}}}trkseg")

    # Sailing fields written as <sail:FIELD> extensions (excludes position/time)
    _SAIL_FIELDS = (
        "HDG",
        "BSP",
        "DEPTH",
        "COG",
        "SOG",
        "TWS",
        "TWA",
        "AWA",
        "AWS",
        "WTEMP",
        "WX_TWS",
        "WX_TWD",
        "AIR_TEMP",
        "PRESSURE",
        "TIDE_HT",
    )

    rows_written = 0
    current = _floor_second(start)
    while current <= end:
        row = _build_row(current, idx)
        lat, lon = row.get("LAT"), row.get("LON")
        if isinstance(lat, float) and isinstance(lon, float):
            trkpt = ET.SubElement(
                trkseg,
                f"{{{_GPX_NS}}}trkpt",
                {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"},
            )
            ET.SubElement(trkpt, f"{{{_GPX_NS}}}ele").text = "0"
            ET.SubElement(trkpt, f"{{{_GPX_NS}}}time").text = current.isoformat()
            sail_data = {k: row[k] for k in _SAIL_FIELDS if isinstance(row.get(k), float)}
            if sail_data:
                ext = ET.SubElement(trkpt, f"{{{_GPX_NS}}}extensions")
                for field_name, val in sail_data.items():
                    assert isinstance(val, float)
                    ET.SubElement(ext, f"{{{_SAIL_NS}}}{field_name}").text = f"{val:.6f}"
        rows_written += 1
        current += timedelta(seconds=1)

    ET.indent(gpx, space="  ")
    tree = ET.ElementTree(gpx)
    with output_path.open("wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(fh, encoding="utf-8", xml_declaration=False)

    logger.info("GPX export complete: {} seconds → {}", rows_written, output_path)
    return rows_written


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


async def export_json(
    storage: Storage,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> int:
    """Export data to a structured JSON file.

    Numeric fields use native JSON number types; missing data is null rather
    than an empty string.

    Returns:
        Number of rows written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    idx = await _load(storage, start, end)
    rows: list[dict[str, float | str | None]] = []

    current = _floor_second(start)
    while current <= end:
        rows.append(_build_row(current, idx))
        current += timedelta(seconds=1)

    doc: dict[str, Any] = {
        "generated": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": rows,
    }

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")

    logger.info("JSON export complete: {} rows → {}", len(rows), output_path)
    return len(rows)


# ---------------------------------------------------------------------------
# Dispatch by extension
# ---------------------------------------------------------------------------


async def export_to_file(
    storage: Storage,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> int:
    """Export data to a file, format inferred from the file extension.

    Supported:
      .csv  — CSV (default for unknown extensions)
      .gpx  — GPX 1.1 track
      .json — structured JSON

    Returns:
        Number of rows/trkpts written (format-dependent).
    """
    suffix = Path(output_path).suffix.lower()
    match suffix:
        case ".gpx":
            return await export_gpx(storage, start, end, output_path)
        case ".json":
            return await export_json(storage, start, end, output_path)
        case _:
            return await export_csv(storage, start, end, output_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0, tzinfo=dt.tzinfo or UTC)


def _second_key(dt: datetime) -> str:
    return dt.isoformat()[:19]


def _hour_key(dt: datetime) -> str:
    return dt.isoformat()[:13] + ":00:00"


def _by_second(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for row in rows:
        idx[row["ts"][:19]] = row
    return idx


def _by_hour(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for row in rows:
        idx[row["ts"][:13] + ":00:00"] = row
    return idx


def _flt(row: dict[str, Any], key: str) -> float | None:
    """Extract a float field from a DB row, returning None if absent."""
    v = row.get(key)
    return float(v) if v is not None else None


def _fmt(value: float | str | None) -> str:
    """Format a value for CSV: None → '', float → 6dp, str → as-is."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return value
