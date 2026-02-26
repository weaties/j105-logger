"""Race lifecycle management — naming logic and configuration.

Pure domain logic only. No database access here; storage methods live in
storage.py. This module is importable without hardware or a running server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass
class Race:
    """A single race window."""

    id: int
    name: str  # e.g. "20250810-BallardCup-2"
    event: str  # e.g. "BallardCup"
    race_num: int
    date: str  # UTC date "YYYY-MM-DD"
    start_utc: datetime
    end_utc: datetime | None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RaceConfig:
    """Web server bind configuration (from environment variables)."""

    web_host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: int(os.environ.get("WEB_PORT", "3002")))


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

_WEEKDAY_EVENTS: dict[int, str] = {
    0: "BallardCup",  # Monday
    2: "CYC",  # Wednesday
}


def default_event_for_date(d: date) -> str | None:
    """Return the default event name for a given UTC date, or None.

    Monday  → "BallardCup"
    Wednesday → "CYC"
    Any other day → None (user must supply the event name)
    """
    return _WEEKDAY_EVENTS.get(d.weekday())


def build_race_name(event: str, d: date, race_num: int) -> str:
    """Build a race identifier string.

    Example: build_race_name("BallardCup", date(2025, 8, 10), 2)
             → "20250810-BallardCup-2"
    """
    return f"{d.strftime('%Y%m%d')}-{event}-{race_num}"
