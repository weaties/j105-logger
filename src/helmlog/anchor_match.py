"""Predicate: does a cursor position "match" an anchor?

Used by:
- UI cursor-highlight logic (mirrored in JS)
- Tests asserting the decision table on #478

Pure function — takes decoded anchor + cursor + a lookup bag for entity
anchors (maneuvers, bookmarks, races) whose payloads live in other
tables. Missing lookup rows return False (defensive: a stale anchor
should not spuriously match).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.anchors import Anchor

DEFAULT_WINDOW_SECONDS: float = 15.0
START_WINDOW_SECONDS: float = 60.0

CursorLike = datetime | str


@dataclass(frozen=True, slots=True)
class Lookups:
    """Resolved entity payloads keyed by id.

    - maneuvers: id -> (ts, end_ts | None) as ISO strings
    - bookmarks: id -> t_start ISO string
    - races:     id -> start_utc ISO string
    """

    maneuvers: dict[int, tuple[str, str | None]] = field(default_factory=dict)
    bookmarks: dict[int, str] = field(default_factory=dict)
    races: dict[int, str] = field(default_factory=dict)


def _to_dt(value: CursorLike | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str):
        # Accept "…Z" suffix as UTC.
        iso = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def anchor_matches_cursor(anchor: Anchor, cursor: CursorLike, lookups: Lookups) -> bool:
    """Return True if `cursor` is within the anchor's active window."""
    c = _to_dt(cursor)
    if c is None:
        return False

    kind = anchor.kind

    if kind == "timestamp":
        t = _to_dt(anchor.t_start)
        if t is None:
            return False
        return abs((c - t).total_seconds()) <= DEFAULT_WINDOW_SECONDS

    if kind == "segment":
        start = _to_dt(anchor.t_start)
        end = _to_dt(anchor.t_end)
        if start is None or end is None:
            return False
        return start <= c < end

    if kind == "maneuver":
        if anchor.entity_id is None:
            return False
        record = lookups.maneuvers.get(anchor.entity_id)
        if record is None:
            return False
        ts_s, end_ts_s = record
        ts = _to_dt(ts_s)
        if ts is None:
            return False
        end_ts = _to_dt(end_ts_s) if end_ts_s is not None else None
        if end_ts is None:
            return abs((c - ts).total_seconds()) <= DEFAULT_WINDOW_SECONDS
        return ts <= c <= end_ts

    if kind == "bookmark":
        if anchor.entity_id is None:
            return False
        t_start_s = lookups.bookmarks.get(anchor.entity_id)
        if t_start_s is None:
            return False
        t = _to_dt(t_start_s)
        if t is None:
            return False
        return abs((c - t).total_seconds()) <= DEFAULT_WINDOW_SECONDS

    if kind == "race":
        # Thread-scope check happens upstream; if the thread is on this
        # session, a race anchor is always active.
        return anchor.entity_id is not None

    if kind == "start":
        if anchor.entity_id is None:
            return False
        start_s = lookups.races.get(anchor.entity_id)
        if start_s is None:
            return False
        start = _to_dt(start_s)
        if start is None:
            return False
        return abs((c - start).total_seconds()) <= START_WINDOW_SECONDS

    return False
