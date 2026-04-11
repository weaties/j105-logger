"""Vakaros VKX binary log parser.

Parses the publicly documented Vakaros VKX telemetry format
(https://github.com/vakaros/vkx) into typed dataclasses. Pure Python, no
hardware required — designed to be tested without a real Atlas device.

Format summary:
    - All fields little-endian.
    - File is a sequence of pages, each ~2 KB. Pages are delimited by:
        0xFF page header (7-byte payload: version + 6 reserved)
        0xFE page terminator (2-byte payload: U2 previous page length)
    - Inside pages: rows keyed by a single byte, with fixed-size payloads.
    - Timestamps are Unix epoch milliseconds (UTC).
"""

from __future__ import annotations

import enum
import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Row keys
# ---------------------------------------------------------------------------

KEY_INTERNAL_01: Final[int] = 0x01  # 32-byte internal message
KEY_POSITION: Final[int] = 0x02  # Position / velocity / orientation
KEY_DECLINATION: Final[int] = 0x03
KEY_RACE_TIMER: Final[int] = 0x04
KEY_LINE_POSITION: Final[int] = 0x05
KEY_SHIFT_ANGLE: Final[int] = 0x06
KEY_INTERNAL_07: Final[int] = 0x07  # 12-byte internal message
KEY_DEVICE_CONFIG: Final[int] = 0x08
KEY_WIND: Final[int] = 0x0A
KEY_SPEED_THROUGH_WATER: Final[int] = 0x0B
KEY_DEPTH: Final[int] = 0x0C
KEY_INTERNAL_0E: Final[int] = 0x0E  # 16-byte internal message
KEY_LOAD: Final[int] = 0x0F
KEY_TEMPERATURE: Final[int] = 0x10
KEY_INTERNAL_20: Final[int] = 0x20  # 13-byte internal message
KEY_INTERNAL_21: Final[int] = 0x21  # 52-byte internal message
KEY_PAGE_TERMINATOR: Final[int] = 0xFE
KEY_PAGE_HEADER: Final[int] = 0xFF

# Payload sizes for each row key (excluding the 1-byte key itself).
ROW_PAYLOAD_SIZES: Final[dict[int, int]] = {
    KEY_INTERNAL_01: 32,
    KEY_POSITION: 44,
    KEY_DECLINATION: 20,
    KEY_RACE_TIMER: 13,
    KEY_LINE_POSITION: 17,
    KEY_SHIFT_ANGLE: 18,
    KEY_INTERNAL_07: 12,
    KEY_DEVICE_CONFIG: 13,
    KEY_WIND: 16,
    KEY_SPEED_THROUGH_WATER: 16,
    KEY_DEPTH: 12,
    KEY_INTERNAL_0E: 16,
    KEY_LOAD: 16,
    KEY_TEMPERATURE: 12,
    KEY_INTERNAL_20: 13,
    KEY_INTERNAL_21: 52,
    KEY_PAGE_HEADER: 7,
    KEY_PAGE_TERMINATOR: 2,
}

# ---------------------------------------------------------------------------
# Decoded row dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionRow:
    """0x02 Position / velocity / orientation."""

    timestamp: datetime
    latitude_deg: float
    longitude_deg: float
    sog_mps: float
    cog_deg: float
    altitude_m: float
    quat_w: float
    quat_x: float
    quat_y: float
    quat_z: float


class RaceTimerEventType(enum.IntEnum):
    RESET = 0
    START = 1
    SYNC = 2
    RACE_START = 3
    RACE_END = 4


@dataclass(frozen=True)
class RaceTimerEvent:
    """0x04 Race timer event — start gun, sync, race end."""

    timestamp: datetime
    event_type: RaceTimerEventType
    timer_value_s: int


class LinePositionType(enum.IntEnum):
    PIN = 0
    BOAT = 1


@dataclass(frozen=True)
class LinePosition:
    """0x05 Start line endpoint ping (pin or committee boat)."""

    timestamp: datetime
    line_type: LinePositionType
    latitude_deg: float
    longitude_deg: float


@dataclass(frozen=True)
class WindRow:
    """0x0A Wind direction + speed."""

    timestamp: datetime
    direction_deg: float
    speed_mps: float


@dataclass(frozen=True)
class VakarosSession:
    """An assembled Vakaros session ready for storage.

    `source_hash` is the SHA-256 hex digest of the raw VKX bytes — used
    as the dedupe key because VKX does not include a device ID.
    """

    source_hash: str
    source_file: str
    start_utc: datetime
    end_utc: datetime
    positions: tuple[PositionRow, ...]
    line_positions: tuple[LinePosition, ...]
    race_events: tuple[RaceTimerEvent, ...]
    winds: tuple[WindRow, ...]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class VKXParseError(ValueError):
    """Raised when a VKX buffer cannot be parsed."""


def _ts_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _decode_position(payload: bytes) -> PositionRow:
    (
        ts_ms,
        raw_lat,
        raw_lon,
        sog_mps,
        cog_rad,
        alt_m,
        qw,
        qx,
        qy,
        qz,
    ) = struct.unpack("<Qiifffffff", payload)
    return PositionRow(
        timestamp=_ts_from_ms(ts_ms),
        latitude_deg=raw_lat * 1e-7,
        longitude_deg=raw_lon * 1e-7,
        sog_mps=sog_mps,
        cog_deg=math.degrees(cog_rad) % 360.0,
        altitude_m=alt_m,
        quat_w=qw,
        quat_x=qx,
        quat_y=qy,
        quat_z=qz,
    )


def _decode_race_timer(payload: bytes) -> RaceTimerEvent:
    ts_ms, raw_type, timer_s = struct.unpack("<QBi", payload)
    return RaceTimerEvent(
        timestamp=_ts_from_ms(ts_ms),
        event_type=RaceTimerEventType(raw_type),
        timer_value_s=timer_s,
    )


def _decode_line_position(payload: bytes) -> LinePosition:
    ts_ms, raw_type, lat_f, lon_f = struct.unpack("<QBff", payload)
    return LinePosition(
        timestamp=_ts_from_ms(ts_ms),
        line_type=LinePositionType(raw_type),
        latitude_deg=lat_f,
        longitude_deg=lon_f,
    )


def _decode_wind(payload: bytes) -> WindRow:
    ts_ms, direction_deg, speed_mps = struct.unpack("<Qff", payload)
    return WindRow(
        timestamp=_ts_from_ms(ts_ms),
        direction_deg=direction_deg,
        speed_mps=speed_mps,
    )


# Decoded-row union type. Internal/unknown rows are skipped (no dataclass).
DecodedRow = PositionRow | RaceTimerEvent | LinePosition | WindRow


def parse_vkx_session(buf: bytes, source_file: str) -> VakarosSession:
    """Parse a VKX buffer into an assembled VakarosSession.

    Raises VKXParseError if the buffer has no Position rows (a session
    with no GPS track is not useful and cannot have a time window).
    """
    import hashlib

    positions: list[PositionRow] = []
    line_positions: list[LinePosition] = []
    race_events: list[RaceTimerEvent] = []
    winds: list[WindRow] = []
    for row in parse_vkx(buf):
        if isinstance(row, PositionRow):
            positions.append(row)
        elif isinstance(row, LinePosition):
            line_positions.append(row)
        elif isinstance(row, RaceTimerEvent):
            race_events.append(row)
        elif isinstance(row, WindRow):
            winds.append(row)
    if not positions:
        raise VKXParseError("VKX file contains no Position rows")
    return VakarosSession(
        source_hash=hashlib.sha256(buf).hexdigest(),
        source_file=source_file,
        start_utc=positions[0].timestamp,
        end_utc=positions[-1].timestamp,
        positions=tuple(positions),
        line_positions=tuple(line_positions),
        race_events=tuple(race_events),
        winds=tuple(winds),
    )


def parse_vkx(buf: bytes) -> Iterator[DecodedRow]:
    """Iterate decoded rows from a VKX byte buffer.

    Unknown and internal rows are consumed but not yielded.
    """
    pos = 0
    n = len(buf)
    while pos < n:
        key = buf[pos]
        size = ROW_PAYLOAD_SIZES.get(key)
        if size is None:
            raise VKXParseError(f"unknown VKX row key 0x{key:02x} at offset {pos}")
        start = pos + 1
        end = start + size
        if end > n:
            raise VKXParseError(
                f"truncated row 0x{key:02x} at offset {pos}: need {size} bytes, have {n - start}"
            )
        payload = buf[start:end]
        if key == KEY_POSITION:
            yield _decode_position(payload)
        elif key == KEY_RACE_TIMER:
            yield _decode_race_timer(payload)
        elif key == KEY_LINE_POSITION:
            yield _decode_line_position(payload)
        elif key == KEY_WIND:
            yield _decode_wind(payload)
        # All other keys (page header/terminator, internal, not-yet-decoded) skip.
        pos = end


async def ingest_vkx_file(storage: Storage, path: Path) -> tuple[int, bool]:
    """Read, parse, store, and match a single VKX file.

    Returns (session_id, was_duplicate). `was_duplicate` is True when a
    session with the same SHA-256 content hash already exists in the
    database — in that case no new rows are written. In both cases the
    session is (re-)matched to any overlapping race so a freshly-ended
    race that started between ingests still gets linked.
    """
    buf = path.read_bytes()
    session = parse_vkx_session(buf, source_file=path.name)
    before_id = await storage.find_vakaros_session_by_hash(session.source_hash)
    session_id = await storage.store_vakaros_session(session)
    await storage.match_vakaros_session(session_id)
    return session_id, before_id is not None
