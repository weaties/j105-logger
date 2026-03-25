"""Signal K WebSocket reader — consumes SK delta feed, emits NMEARecord types.

Replaces can_reader.py when Signal K Server owns the CAN bus.
Emits the same record types as nmea2000.py so storage and export are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from websockets.asyncio.client import connect as _ws_connect

from helmlog.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_ENVIRONMENTAL,
    PGN_POSITION_RAPID,
    PGN_RUDDER_ANGLE,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WATER_DEPTH,
    PGN_WIND_DATA,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PGNRecord,
    PositionRecord,
    RudderRecord,
    SpeedRecord,
    WindRecord,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

_RAD_TO_DEG: float = 180.0 / math.pi
_MPS_TO_KTS: float = 1.94384449
_KELVIN_OFFSET: float = 273.15
SK_SOURCE_ADDR: int = 0  # no CAN source address for SK-originated records


@dataclass
class SKReaderConfig:
    """Configuration for the Signal K WebSocket reader.

    Values fall back to environment variables SK_HOST / SK_PORT if not set.
    Auth waterfall: SK_TOKEN → SK_USERNAME/SK_PASSWORD → ~/.signalk-admin-pass.txt.
    """

    host: str = field(default_factory=lambda: os.environ.get("SK_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.environ.get("SK_PORT", "3000")))
    reconnect_delay_s: float = 5.0
    token: str | None = field(default_factory=lambda: os.environ.get("SK_TOKEN"))
    username: str | None = field(default_factory=lambda: os.environ.get("SK_USERNAME"))
    password: str | None = field(default_factory=lambda: os.environ.get("SK_PASSWORD"))


# ---------------------------------------------------------------------------
# Simple single-value record constructors
# ---------------------------------------------------------------------------


def _mk_heading(v: float, ts: datetime) -> HeadingRecord:
    return HeadingRecord(PGN_VESSEL_HEADING, SK_SOURCE_ADDR, ts, v * _RAD_TO_DEG, None, None)


def _mk_speed(v: float, ts: datetime) -> SpeedRecord:
    return SpeedRecord(PGN_SPEED_THROUGH_WATER, SK_SOURCE_ADDR, ts, v * _MPS_TO_KTS)


def _mk_depth(v: float, ts: datetime) -> DepthRecord:
    return DepthRecord(PGN_WATER_DEPTH, SK_SOURCE_ADDR, ts, v, None)


def _mk_env(v: float, ts: datetime) -> EnvironmentalRecord:
    return EnvironmentalRecord(PGN_ENVIRONMENTAL, SK_SOURCE_ADDR, ts, v - _KELVIN_OFFSET)


def _mk_rudder(v: float, ts: datetime) -> RudderRecord:
    return RudderRecord(PGN_RUDDER_ANGLE, SK_SOURCE_ADDR, ts, v * _RAD_TO_DEG)


# ---------------------------------------------------------------------------
# Paired record builders (buffer until both values arrive)
# ---------------------------------------------------------------------------


def _try_cogsog(buf: dict[str, float], ts: datetime) -> COGSOGRecord | None:
    cog = buf.get("navigation.courseOverGroundTrue")
    sog = buf.get("navigation.speedOverGround")
    if cog is None or sog is None:
        return None
    return COGSOGRecord(PGN_COG_SOG_RAPID, SK_SOURCE_ADDR, ts, cog * _RAD_TO_DEG, sog * _MPS_TO_KTS)


def _try_true_wind(buf: dict[str, float], ts: datetime) -> WindRecord | None:
    spd = buf.get("environment.wind.speedTrue")
    if spd is None:
        return None
    # Prefer boat-referenced angle (TWA, reference=0); try all Signal K variants
    for ang_key in (
        "environment.wind.angleTrue",
        "environment.wind.angleTrueWater",
        "environment.wind.angleTrueGround",
    ):
        ang = buf.get(ang_key)
        if ang is not None:
            return WindRecord(
                PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, ang * _RAD_TO_DEG, 0
            )
    # Fall back to north-referenced direction (TWD, reference=4) — common on B&G
    direction = buf.get("environment.wind.directionTrue")
    if direction is not None:
        return WindRecord(
            PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, direction * _RAD_TO_DEG, 4
        )
    return None


def _try_app_wind(buf: dict[str, float], ts: datetime) -> WindRecord | None:
    spd = buf.get("environment.wind.speedApparent")
    ang = buf.get("environment.wind.angleApparent")
    if spd is None or ang is None:
        return None
    return WindRecord(PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, ang * _RAD_TO_DEG, 2)


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

_SIMPLE: dict[str, Callable[[float, datetime], PGNRecord]] = {
    "navigation.headingTrue": _mk_heading,
    "navigation.speedThroughWater": _mk_speed,
    "environment.depth.belowKeel": _mk_depth,
    "environment.water.temperature": _mk_env,
    "steering.rudderAngle": _mk_rudder,
}

_PAIR: dict[str, Callable[[dict[str, float], datetime], PGNRecord | None]] = {
    "navigation.courseOverGroundTrue": _try_cogsog,
    "navigation.speedOverGround": _try_cogsog,
    "environment.wind.speedTrue": _try_true_wind,
    "environment.wind.angleTrue": _try_true_wind,
    "environment.wind.angleTrueWater": _try_true_wind,
    "environment.wind.angleTrueGround": _try_true_wind,
    "environment.wind.directionTrue": _try_true_wind,
    "environment.wind.speedApparent": _try_app_wind,
    "environment.wind.angleApparent": _try_app_wind,
}


# ---------------------------------------------------------------------------
# Delta parser (pure function — testable without a WebSocket)
# ---------------------------------------------------------------------------


def process_delta(
    raw: str, buf: dict[str, float], *, self_context: str | None = None
) -> list[PGNRecord]:
    """Parse a Signal K delta message; return any records it produces.

    Updates *buf* in-place for multi-field records (COG+SOG, wind speed+angle).
    Unknown paths are silently ignored at DEBUG level.
    Malformed numeric values are logged at WARNING and skipped.

    *self_context* is the resolved self-vessel context from the SK API
    (e.g. ``"vessels.urn:mrn:signalk:uuid:..."``) so UUID-style contexts are
    accepted when they match.
    """
    try:
        delta: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("SK: malformed JSON: {}", exc)
        return []

    records: list[PGNRecord] = []

    # Reject other-vessel data — only process self-vessel deltas (#208)
    context: str = delta.get("context", "vessels.self")
    if (
        context
        and context != "vessels.self"
        and not context.endswith(".self")
        and (not self_context or context != self_context)
    ):
        logger.warning("SK: rejecting non-self delta (context={!r})", context)
        return []

    for update in delta.get("updates", []):
        ts_str: str = update.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.now(UTC)

        for entry in update.get("values", []):
            path: str = entry.get("path", "")
            value: Any = entry.get("value")
            if value is None:
                continue

            # Block AIS-related paths (#208)
            if "ais" in path.lower() or path.startswith("vessels.urn:"):
                logger.warning("SK: rejecting AIS/other-vessel path {!r}", path)
                continue

            if path == "navigation.position":
                try:
                    records.append(
                        PositionRecord(
                            PGN_POSITION_RAPID,
                            SK_SOURCE_ADDR,
                            ts,
                            float(value["latitude"]),
                            float(value["longitude"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("SK: bad position value {!r}: {}", value, exc)
                continue

            if simple_fn := _SIMPLE.get(path):
                try:
                    records.append(simple_fn(float(value), ts))
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            if pair_fn := _PAIR.get(path):
                try:
                    buf[path] = float(value)
                    rec = pair_fn(buf, ts)
                    if rec is not None:
                        records.append(rec)
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            logger.debug("SK: unknown path {!r} — ignoring", path)

    return records


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class SKReader:
    """Async iterable that yields NMEARecord instances from a Signal K WebSocket.

    Reconnects automatically on disconnect using exponential backoff capped at
    ``config.reconnect_delay_s``.  Propagates ``asyncio.CancelledError`` cleanly.

    Usage::

        async for record in SKReader(SKReaderConfig()):
            await storage.write(record)
    """

    def __init__(self, config: SKReaderConfig) -> None:
        self._config = config
        self._buf: dict[str, float] = {}
        self._self_context: str | None = None
        self._token: str | None = None

    def __aiter__(self) -> AsyncIterator[PGNRecord]:
        return self._stream()

    async def _resolve_token(self) -> str | None:
        """Resolve a Signal K auth token using the waterfall:

        1. Explicit token from config (SK_TOKEN env var)
        2. Login with username/password (SK_USERNAME / SK_PASSWORD env vars)
        3. Read password from ~/.signalk-admin-pass.txt (written by setup.sh)
        4. None (unauthenticated — works only if SK security is disabled)
        """
        import httpx

        config = self._config

        # 1. Explicit token
        if config.token:
            self._token = config.token
            logger.info("SK: using explicit auth token")
            return self._token

        # 2. Credentials from config/env, or 3. fall back to password file
        username = config.username
        password = config.password
        if not username or not password:
            pass_file = Path.home() / ".signalk-admin-pass.txt"
            try:
                password = pass_file.read_text().strip()
                username = "admin"
                logger.debug("SK: read password from {}", pass_file)
            except FileNotFoundError:
                logger.debug("SK: no password file at {}", pass_file)
                return None

        # Login via SK REST API
        url = f"http://{config.host}:{config.port}/signalk/v1/auth/login"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url, json={"username": username, "password": password}, timeout=5.0
                )
                resp.raise_for_status()
                self._token = resp.json().get("token")
                if self._token:
                    logger.info("SK: authenticated as {!r}", username)
                    return self._token
                logger.warning("SK: login response missing token field")
        except Exception as exc:
            logger.warning("SK: auth login failed: {}", exc)
        return None

    async def _resolve_self_context(self) -> str | None:
        """Fetch the self-vessel context from the Signal K REST API."""
        import httpx

        config = self._config
        url = f"http://{config.host}:{config.port}/signalk/v1/api/self"
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5.0, headers=headers)
                resp.raise_for_status()
                # Response is a JSON string like "vessels.urn:mrn:signalk:uuid:..."
                ctx = resp.json()
                if isinstance(ctx, str) and ctx:
                    logger.info("SK: resolved self context → {}", ctx)
                    return ctx
        except Exception as exc:
            logger.warning("SK: could not resolve self context from {}: {}", url, exc)
        return None

    async def _stream(self) -> AsyncGenerator[PGNRecord, None]:
        config = self._config
        base_uri = f"ws://{config.host}:{config.port}/signalk/v1/stream?subscribe=all"
        delay = 1.0
        while True:
            try:
                # Resolve auth token if not yet obtained
                if self._token is None:
                    await self._resolve_token()
                # Resolve self-vessel context on first connect or if not yet known
                if self._self_context is None:
                    self._self_context = await self._resolve_self_context()
                uri = f"{base_uri}&token={self._token}" if self._token else base_uri
                logger.info("SK: connecting to {}", base_uri)
                async with _ws_connect(uri) as ws:
                    delay = 1.0
                    logger.info("SK: connected")
                    async for raw in ws:
                        for record in process_delta(
                            str(raw), self._buf, self_context=self._self_context
                        ):
                            yield record
            except asyncio.CancelledError:
                logger.info("SK: cancelled — stopping")
                raise
            except Exception as exc:
                logger.warning("SK: connection error ({}). Reconnecting in {:.1f}s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, config.reconnect_delay_s)
