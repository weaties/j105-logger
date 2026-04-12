"""Tests for the Vakaros VKX binary log parser."""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime

import pytest


def _build_position_payload(
    timestamp_ms: int,
    lat_deg: float,
    lon_deg: float,
    sog_mps: float,
    cog_rad: float,
    altitude_m: float,
    quat: tuple[float, float, float, float],
) -> bytes:
    """Pack a 44-byte VKX 0x02 (Position/Velocity/Orientation) payload."""
    return struct.pack(
        "<Qiifffffff",  # U8, I4, I4, F4*7
        timestamp_ms,
        round(lat_deg / 1e-7),
        round(lon_deg / 1e-7),
        sog_mps,
        cog_rad,
        altitude_m,
        quat[0],
        quat[1],
        quat[2],
        quat[3],
    )


def _build_minimal_vkx(rows: bytes) -> bytes:
    """Wrap row bytes in a VKX page header (0xFF) + page terminator (0xFE)."""
    # Page header: key 0xFF, payload = 7 bytes (version 0x05 + 6 reserved)
    header = bytes([0xFF, 0x05, 0, 0, 0, 0, 0, 0])
    # Page terminator: key 0xFE, payload = U2 previous page length (rows length)
    terminator = bytes([0xFE]) + struct.pack("<H", len(rows))
    return header + rows + terminator


@pytest.mark.asyncio
async def test_parse_vkx_decodes_single_position_row() -> None:
    from helmlog.vakaros import PositionRow, parse_vkx

    ts_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    payload = _build_position_payload(
        timestamp_ms=ts_ms,
        lat_deg=37.8044,
        lon_deg=-122.2712,
        sog_mps=3.5,
        cog_rad=math.radians(45.0),
        altitude_m=12.0,
        quat=(1.0, 0.0, 0.0, 0.0),
    )
    row_bytes = bytes([0x02]) + payload
    buf = _build_minimal_vkx(row_bytes)

    rows = list(parse_vkx(buf))

    position_rows = [r for r in rows if isinstance(r, PositionRow)]
    assert len(position_rows) == 1
    p = position_rows[0]
    assert p.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert p.latitude_deg == pytest.approx(37.8044, abs=1e-6)
    assert p.longitude_deg == pytest.approx(-122.2712, abs=1e-6)
    assert p.sog_mps == pytest.approx(3.5)
    assert p.cog_deg == pytest.approx(45.0, abs=1e-3)
    assert p.altitude_m == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_parse_vkx_decodes_race_timer_event() -> None:
    from helmlog.vakaros import RaceTimerEvent, RaceTimerEventType, parse_vkx

    ts_ms = 1_700_000_005_000
    payload = struct.pack("<QBi", ts_ms, 3, -300)  # RACE_START, T-5:00
    buf = _build_minimal_vkx(bytes([0x04]) + payload)

    rows = [r for r in parse_vkx(buf) if isinstance(r, RaceTimerEvent)]
    assert len(rows) == 1
    e = rows[0]
    assert e.timestamp == datetime(2023, 11, 14, 22, 13, 25, tzinfo=UTC)
    assert e.event_type is RaceTimerEventType.RACE_START
    assert e.timer_value_s == -300


@pytest.mark.asyncio
async def test_parse_vkx_decodes_line_position_pin_and_boat() -> None:
    from helmlog.vakaros import LinePosition, LinePositionType, parse_vkx

    ts_ms = 1_700_000_010_000
    pin_payload = struct.pack("<QBff", ts_ms, 0, 37.8050, -122.2700)
    boat_payload = struct.pack("<QBff", ts_ms + 1000, 1, 37.8048, -122.2705)
    buf = _build_minimal_vkx(bytes([0x05]) + pin_payload + bytes([0x05]) + boat_payload)

    line_rows = [r for r in parse_vkx(buf) if isinstance(r, LinePosition)]
    assert len(line_rows) == 2
    pin, boat = line_rows
    assert pin.line_type is LinePositionType.PIN
    assert pin.latitude_deg == pytest.approx(37.8050, abs=1e-4)
    assert pin.longitude_deg == pytest.approx(-122.2700, abs=1e-4)
    assert boat.line_type is LinePositionType.BOAT
    assert boat.latitude_deg == pytest.approx(37.8048, abs=1e-4)


@pytest.mark.asyncio
async def test_parse_vkx_decodes_wind() -> None:
    from helmlog.vakaros import WindRow, parse_vkx

    ts_ms = 1_700_000_020_000
    payload = struct.pack("<Qff", ts_ms, 215.0, 7.5)  # 215° at 7.5 m/s
    buf = _build_minimal_vkx(bytes([0x0A]) + payload)

    wind_rows = [r for r in parse_vkx(buf) if isinstance(r, WindRow)]
    assert len(wind_rows) == 1
    w = wind_rows[0]
    assert w.direction_deg == pytest.approx(215.0)
    assert w.speed_mps == pytest.approx(7.5)


@pytest.mark.asyncio
async def test_parse_vkx_skips_unknown_internal_rows_silently() -> None:
    from helmlog.vakaros import PositionRow, parse_vkx

    # 0x07 internal message (12-byte payload) sandwiched between two positions
    pos1 = bytes([0x02]) + _build_position_payload(
        1_700_000_000_000, 37.8, -122.27, 1.0, 0.0, 0.0, (1.0, 0.0, 0.0, 0.0)
    )
    internal = bytes([0x07]) + bytes(12)
    pos2 = bytes([0x02]) + _build_position_payload(
        1_700_000_001_000, 37.8001, -122.27, 1.0, 0.0, 0.0, (1.0, 0.0, 0.0, 0.0)
    )
    buf = _build_minimal_vkx(pos1 + internal + pos2)

    positions = [r for r in parse_vkx(buf) if isinstance(r, PositionRow)]
    assert len(positions) == 2


@pytest.mark.asyncio
async def test_parse_vkx_raises_on_unknown_row_key() -> None:
    from helmlog.vakaros import VKXParseError, parse_vkx

    # 0xAB is not a defined row key
    buf = _build_minimal_vkx(bytes([0xAB, 0, 0, 0]))
    with pytest.raises(VKXParseError, match="unknown VKX row key"):
        list(parse_vkx(buf))


@pytest.mark.asyncio
async def test_parse_vkx_raises_on_truncated_row() -> None:
    from helmlog.vakaros import VKXParseError, parse_vkx

    # Position row claims 44 bytes but only 10 are present.
    bad = bytes([0x02]) + bytes(10)
    # Don't wrap in page header — terminator would otherwise consume the trailing bytes.
    with pytest.raises(VKXParseError, match="truncated"):
        list(parse_vkx(bad))
