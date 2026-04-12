"""Session matching decision table (#459).

Maps the decision-table rows in the spec comment to test cases.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from helmlog.results.base import (
    BoatFinish,
    RaceData,
    Regatta,
    RegattaResults,
    ResultsProvider,
    get_provider,
    register_provider,
)
from helmlog.results.session_match import (
    SessionCandidate,
    SessionMatchOutcome,
    match_race_to_sessions,
)

_PT = ZoneInfo("America/Los_Angeles")
_RACE_DATE = date(2026, 6, 15)


def _sess(id_: int, when: datetime, label: str = "") -> SessionCandidate:
    return SessionCandidate(id=id_, start_utc=when, label=label or f"s{id_}")


# ---------------------------------------------------------------------------
# Decision table
# ---------------------------------------------------------------------------


def test_zero_candidates_no_match() -> None:
    result = match_race_to_sessions(_RACE_DATE, [], _PT)
    assert result.outcome == SessionMatchOutcome.NO_MATCH
    assert result.auto_linked_id is None
    assert result.candidates == ()


def test_one_candidate_auto_match() -> None:
    # 2026-06-15 12:00 local (PT = UTC-7 in June) → 19:00 UTC
    sess = _sess(1, datetime(2026, 6, 15, 19, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [sess], _PT)
    assert result.outcome == SessionMatchOutcome.AUTO_MATCH
    assert result.auto_linked_id == 1


def test_two_candidates_ambiguous() -> None:
    a = _sess(1, datetime(2026, 6, 15, 16, 0, tzinfo=ZoneInfo("UTC")))
    b = _sess(2, datetime(2026, 6, 15, 22, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [a, b], _PT)
    assert result.outcome == SessionMatchOutcome.AMBIGUOUS
    assert result.auto_linked_id is None
    assert len(result.candidates) == 2


def test_three_or_more_candidates_ambiguous() -> None:
    sessions = [
        _sess(1, datetime(2026, 6, 15, 16, 0, tzinfo=ZoneInfo("UTC"))),
        _sess(2, datetime(2026, 6, 15, 19, 0, tzinfo=ZoneInfo("UTC"))),
        _sess(3, datetime(2026, 6, 15, 22, 0, tzinfo=ZoneInfo("UTC"))),
    ]
    result = match_race_to_sessions(_RACE_DATE, sessions, _PT)
    assert result.outcome == SessionMatchOutcome.AMBIGUOUS
    assert len(result.candidates) == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_session_in_different_venue_date_filtered_out() -> None:
    # Session at 2026-06-14 20:00 PT → UTC is still 2026-06-15 03:00
    # Local date = 2026-06-14, not the race date → filtered out.
    sess = _sess(1, datetime(2026, 6, 15, 3, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [sess], _PT)
    assert result.outcome == SessionMatchOutcome.NO_MATCH


def test_session_straddling_utc_midnight_counts_when_local_date_matches() -> None:
    # Session starts 2026-06-15 23:00 PT → 2026-06-16 06:00 UTC.
    # Venue local date = 2026-06-15 → counts as a match.
    sess = _sess(1, datetime(2026, 6, 16, 6, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [sess], _PT)
    assert result.outcome == SessionMatchOutcome.AUTO_MATCH
    assert result.auto_linked_id == 1


def test_future_race_date_with_only_past_sessions_no_match() -> None:
    past = _sess(1, datetime(2020, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [past], _PT)
    assert result.outcome == SessionMatchOutcome.NO_MATCH


def test_candidates_sorted_by_start_time() -> None:
    later = _sess(1, datetime(2026, 6, 15, 22, 0, tzinfo=ZoneInfo("UTC")))
    earlier = _sess(2, datetime(2026, 6, 15, 16, 0, tzinfo=ZoneInfo("UTC")))
    result = match_race_to_sessions(_RACE_DATE, [later, earlier], _PT)
    assert [c.id for c in result.candidates] == [2, 1]


# ---------------------------------------------------------------------------
# Provider registry (R3, R4)
# ---------------------------------------------------------------------------


class _StubProvider:
    source_name = "stub"

    async def fetch(self, regatta: Regatta) -> RegattaResults:
        return RegattaResults(regatta=regatta)


def test_register_and_lookup_provider() -> None:
    assert isinstance(_StubProvider(), ResultsProvider)
    register_provider(_StubProvider())
    found = get_provider("stub")
    assert found is not None
    assert found.source_name == "stub"


def test_unknown_provider_returns_none() -> None:
    assert get_provider("never-registered-xyz") is None


def test_dataclass_defaults_are_immutable() -> None:
    """Frozen dataclasses catch accidental mutation in the importer."""
    finish = BoatFinish(sail_number="USA 123", place=1)
    race = RaceData(
        source_id="r1",
        race_number=1,
        name="Race 1",
        date="2026-06-15",
        class_name="J/105",
        finishes=(finish,),
    )
    # frozen=True → attribute assignment raises.
    try:
        finish.place = 2  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("BoatFinish should be frozen")
    assert race.finishes[0].place == 1
