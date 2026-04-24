"""Unit tests for the cursor-vs-anchor match predicate (decision table 2 on #478).

| Anchor kind | Match predicate                                           | Window    |
|-------------|-----------------------------------------------------------|-----------|
| timestamp   | abs(cursor - t_start) <= W                                | 15s       |
| segment     | t_start <= cursor < t_end                                 | exact     |
| maneuver    | maneuver.ts <= cursor <= maneuver.end_ts (fallback ±W)    | exact/15s |
| race        | always active on the thread's session                     | -         |
| start       | abs(cursor - race.start_utc) <= W_start                   | 60s       |
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from helmlog.anchor_match import (
    DEFAULT_WINDOW_SECONDS,
    START_WINDOW_SECONDS,
    Lookups,
    anchor_matches_cursor,
)
from helmlog.anchors import Anchor

_BASE = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _at(offset_seconds: float) -> datetime:
    return _BASE + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# timestamp — point ± 15s
# ---------------------------------------------------------------------------


def test_timestamp_matches_within_window() -> None:
    a = Anchor(kind="timestamp", t_start=_iso(_BASE))
    assert anchor_matches_cursor(a, _at(DEFAULT_WINDOW_SECONDS - 1), Lookups())
    assert anchor_matches_cursor(a, _at(-(DEFAULT_WINDOW_SECONDS - 1)), Lookups())


def test_timestamp_misses_outside_window() -> None:
    a = Anchor(kind="timestamp", t_start=_iso(_BASE))
    assert not anchor_matches_cursor(a, _at(DEFAULT_WINDOW_SECONDS + 1), Lookups())


def test_timestamp_at_window_boundary_matches() -> None:
    a = Anchor(kind="timestamp", t_start=_iso(_BASE))
    assert anchor_matches_cursor(a, _at(DEFAULT_WINDOW_SECONDS), Lookups())


# ---------------------------------------------------------------------------
# segment — [t_start, t_end)
# ---------------------------------------------------------------------------


def test_segment_matches_inside() -> None:
    a = Anchor(kind="segment", t_start=_iso(_BASE), t_end=_iso(_at(30)))
    assert anchor_matches_cursor(a, _at(15), Lookups())


def test_segment_matches_at_start() -> None:
    a = Anchor(kind="segment", t_start=_iso(_BASE), t_end=_iso(_at(30)))
    assert anchor_matches_cursor(a, _BASE, Lookups())


def test_segment_misses_at_end_boundary() -> None:
    """End is exclusive — cursor == t_end is not a match."""
    a = Anchor(kind="segment", t_start=_iso(_BASE), t_end=_iso(_at(30)))
    assert not anchor_matches_cursor(a, _at(30), Lookups())


def test_segment_misses_before_start() -> None:
    a = Anchor(kind="segment", t_start=_iso(_BASE), t_end=_iso(_at(30)))
    assert not anchor_matches_cursor(a, _at(-1), Lookups())


# ---------------------------------------------------------------------------
# maneuver — resolved from lookups
# ---------------------------------------------------------------------------


def test_maneuver_matches_within_range() -> None:
    a = Anchor(kind="maneuver", entity_id=42)
    lookups = Lookups(maneuvers={42: (_iso(_BASE), _iso(_at(10)))})
    assert anchor_matches_cursor(a, _at(5), lookups)


def test_maneuver_matches_null_end_ts_with_window() -> None:
    """When end_ts is null, fall back to ±15s around ts."""
    a = Anchor(kind="maneuver", entity_id=42)
    lookups = Lookups(maneuvers={42: (_iso(_BASE), None)})
    assert anchor_matches_cursor(a, _at(DEFAULT_WINDOW_SECONDS - 1), lookups)
    assert not anchor_matches_cursor(a, _at(DEFAULT_WINDOW_SECONDS + 1), lookups)


def test_maneuver_misses_when_not_in_lookups() -> None:
    a = Anchor(kind="maneuver", entity_id=999)
    assert not anchor_matches_cursor(a, _BASE, Lookups())


# ---------------------------------------------------------------------------
# race — always active on the thread's session
# ---------------------------------------------------------------------------


def test_race_always_active() -> None:
    a = Anchor(kind="race", entity_id=7)
    assert anchor_matches_cursor(a, _at(-3600), Lookups())
    assert anchor_matches_cursor(a, _at(3600), Lookups())


# ---------------------------------------------------------------------------
# start — ± 60s around race.start_utc
# ---------------------------------------------------------------------------


def test_start_matches_within_start_window() -> None:
    a = Anchor(kind="start", entity_id=7)
    lookups = Lookups(races={7: _iso(_BASE)})
    assert anchor_matches_cursor(a, _at(START_WINDOW_SECONDS - 1), lookups)
    assert anchor_matches_cursor(a, _at(-(START_WINDOW_SECONDS - 1)), lookups)


def test_start_misses_outside_start_window() -> None:
    a = Anchor(kind="start", entity_id=7)
    lookups = Lookups(races={7: _iso(_BASE)})
    assert not anchor_matches_cursor(a, _at(START_WINDOW_SECONDS + 1), lookups)


def test_start_misses_when_race_not_in_lookups() -> None:
    a = Anchor(kind="start", entity_id=99)
    assert not anchor_matches_cursor(a, _BASE, Lookups())


# ---------------------------------------------------------------------------
# Invalid / missing data
# ---------------------------------------------------------------------------


def test_timestamp_without_t_start_misses() -> None:
    """Defensive: a malformed anchor should silently not match."""
    a = Anchor(kind="timestamp")
    assert not anchor_matches_cursor(a, _BASE, Lookups())


def test_unknown_kind_misses() -> None:
    a = Anchor(kind="bogus")
    assert not anchor_matches_cursor(a, _BASE, Lookups())


@pytest.mark.parametrize(
    "iso_cursor",
    ["2024-06-15T12:00:00+00:00", "2024-06-15T12:00:00Z"],
)
def test_accepts_string_cursor(iso_cursor: str) -> None:
    """Callers may pass a string cursor (datetime or ISO-Z)."""
    a = Anchor(kind="timestamp", t_start=_iso(_BASE))
    assert anchor_matches_cursor(a, iso_cursor, Lookups())
