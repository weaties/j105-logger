"""Tests for export.py — CSV export."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from logger.export import export_csv
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
    from pathlib import Path

    from logger.storage import Storage

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_END = _TS + timedelta(seconds=2)  # 3-second window → 3 rows

# Expected CSV columns
_EXPECTED_COLS = {
    "timestamp",
    "HDG",
    "BSP",
    "DEPTH",
    "LAT",
    "LON",
    "COG",
    "SOG",
    "TWS",
    "TWA",
    "AWA",
    "AWS",
    "WTEMP",
}


async def _populate(storage: Storage) -> None:
    """Write one record of each type at _TS."""
    records = [
        HeadingRecord(PGN_VESSEL_HEADING, 5, _TS, 180.0, None, None),
        SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, _TS, 5.0),
        DepthRecord(PGN_WATER_DEPTH, 5, _TS, 10.0, None),
        PositionRecord(PGN_POSITION_RAPID, 5, _TS, 37.8044, -122.2712),
        COGSOGRecord(PGN_COG_SOG_RAPID, 5, _TS, 45.0, 6.0),
        WindRecord(PGN_WIND_DATA, 5, _TS, 15.0, 30.0, 0),  # true wind
        EnvironmentalRecord(PGN_ENVIRONMENTAL, 5, _TS, 20.0),
    ]
    for r in records:
        await storage.write(r)


class TestExportCSV:
    async def test_columns_present(self, storage: Storage, tmp_path: Path) -> None:
        """Output CSV must have all expected columns."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames or []) == _EXPECTED_COLS

    async def test_row_count_matches_seconds(self, storage: Storage, tmp_path: Path) -> None:
        """3-second window [T, T+2] → 3 rows."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        rows_written = await export_csv(storage, _TS, _END, out)
        assert rows_written == 3

        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 3

    async def test_first_row_has_data(self, storage: Storage, tmp_path: Path) -> None:
        """First row (at _TS) should have populated values."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        first = rows[0]
        assert first["HDG"] != ""
        assert first["BSP"] != ""
        assert first["LAT"] != ""
        assert first["LON"] != ""
        assert first["WTEMP"] != ""

    async def test_missing_data_produces_empty_cells(
        self, storage: Storage, tmp_path: Path
    ) -> None:
        """Seconds with no data should have empty (not erroring) cells."""
        # Only write data at _TS; seconds _TS+1 and _TS+2 have nothing
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        # Second row (T+1) should have empty numeric fields
        second = rows[1]
        assert second["HDG"] == ""
        assert second["BSP"] == ""

    async def test_timestamp_ordering(self, storage: Storage, tmp_path: Path) -> None:
        """Timestamps must be strictly ascending in output."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        timestamps = [datetime.fromisoformat(r["timestamp"]) for r in rows]
        assert timestamps == sorted(timestamps)

    async def test_empty_range_writes_header_only(self, storage: Storage, tmp_path: Path) -> None:
        """Export on range with no data still writes a valid CSV with header."""
        out = tmp_path / "export.csv"
        # Use a past time range with nothing in the DB
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 1, tzinfo=UTC)  # single second
        rows_written = await export_csv(storage, start, end, out)
        assert rows_written == 1  # one second = one row (all empty)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames or []) == _EXPECTED_COLS

    async def test_true_wind_vs_apparent_wind(self, storage: Storage, tmp_path: Path) -> None:
        """True wind (ref=0) → TWS/TWA; apparent wind (ref=2) → AWS/AWA."""
        # True wind
        await storage.write(WindRecord(PGN_WIND_DATA, 5, _TS, 15.0, 30.0, 0))
        # Apparent wind
        await storage.write(WindRecord(PGN_WIND_DATA, 5, _TS, 12.0, 25.0, 2))
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        row = rows[0]
        assert row["TWS"] != ""
        assert row["AWA"] != ""
        assert row["TWS"] != row["AWS"]

    async def test_output_dir_created(self, storage: Storage, tmp_path: Path) -> None:
        """Export should create output directory if it doesn't exist."""
        out = tmp_path / "nested" / "deep" / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        assert out.exists()

    async def test_numeric_values_correct(self, storage: Storage, tmp_path: Path) -> None:
        """Spot-check a numeric value in the CSV."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        row = rows[0]
        assert abs(float(row["HDG"]) - 180.0) < 0.01
        assert abs(float(row["BSP"]) - 5.0) < 0.01
        assert abs(float(row["DEPTH"]) - 10.0) < 0.01
        assert abs(float(row["WTEMP"]) - 20.0) < 0.01
