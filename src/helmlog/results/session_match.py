"""Match imported races to local helmlog sessions by date.

Implements the decision table from the #459 spec:

    0 candidates → NO_MATCH, local_session_id left NULL
    1 candidate  → AUTO_MATCH, local_session_id auto-linked
    2+ candidates → AMBIGUOUS, local_session_id left NULL (admin picks)

Matching uses the race's local date (venue timezone) against each local
session's start_utc *converted to the venue local date*. A session that
straddles midnight UTC but is on the race date in venue local time counts
as a match — this is why we take the venue timezone as an explicit
argument and don't compare raw UTC strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime
    from zoneinfo import ZoneInfo


class SessionMatchOutcome(Enum):
    """Result of matching one race to local sessions."""

    NO_MATCH = "no_match"
    AUTO_MATCH = "auto_match"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SessionCandidate:
    """A local race row that is a candidate for matching against an import."""

    id: int
    start_utc: datetime
    label: str


@dataclass(frozen=True)
class SessionMatch:
    """Outcome of matching one imported race to local sessions."""

    outcome: SessionMatchOutcome
    auto_linked_id: int | None
    candidates: tuple[SessionCandidate, ...]


def _as_local_date(dt: datetime, tz: ZoneInfo) -> date:
    """Convert an aware datetime to a calendar date in `tz`."""
    if dt.tzinfo is None:
        raise ValueError("session start_utc must be timezone-aware")
    return dt.astimezone(tz).date()


def match_race_to_sessions(
    race_date: date,
    sessions: list[SessionCandidate],
    venue_tz: ZoneInfo,
) -> SessionMatch:
    """Return the match outcome for one imported race.

    Arguments:
        race_date: The race's local date (already parsed to a calendar date).
        sessions: All local sessions to consider. The caller typically passes
            sessions within ±1 day of `race_date`; this function filters to
            those that fall on the exact local date.
        venue_tz: The regatta's venue timezone — used to convert each
            session's UTC timestamp to a local date.
    """
    matches = [s for s in sessions if _as_local_date(s.start_utc, venue_tz) == race_date]
    matches.sort(key=lambda s: s.start_utc)

    if len(matches) == 0:
        return SessionMatch(
            outcome=SessionMatchOutcome.NO_MATCH,
            auto_linked_id=None,
            candidates=(),
        )
    if len(matches) == 1:
        return SessionMatch(
            outcome=SessionMatchOutcome.AUTO_MATCH,
            auto_linked_id=matches[0].id,
            candidates=tuple(matches),
        )
    return SessionMatch(
        outcome=SessionMatchOutcome.AMBIGUOUS,
        auto_linked_id=None,
        candidates=tuple(matches),
    )
