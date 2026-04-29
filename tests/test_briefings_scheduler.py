"""Tests for the briefing scheduler tick computation (#700)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from helmlog.briefings import (
    BriefingTick,
    VenueConfig,
    next_tick,
    ticks_for_date,
)

PT = ZoneInfo("America/Los_Angeles")
SHILSHOLE = VenueConfig(
    venue_id="shilshole",
    venue_name="Shilshole Bay",
    venue_lat=47.6800,
    venue_lon=-122.4067,
    venue_tz="America/Los_Angeles",
    days_of_week=(0, 2),
    racing_window_local=(time(18, 0), time(21, 0)),
    lead_hours=(12, 8, 6, 4, 2, 0),
)


def test_ticks_for_date_returns_six_ticks_on_a_race_day() -> None:
    # 2026-04-27 is a Monday.
    monday = date(2026, 4, 27)
    ticks = ticks_for_date(SHILSHOLE, monday)
    assert len(ticks) == 6
    assert [t.lead_hours for t in ticks] == [12, 8, 6, 4, 2, 0]
    for t in ticks:
        assert t.venue_id == "shilshole"
        assert t.local_date == monday


def test_ticks_for_date_returns_empty_on_non_race_day() -> None:
    # 2026-04-28 is a Tuesday — not in days_of_week.
    tuesday = date(2026, 4, 28)
    assert ticks_for_date(SHILSHOLE, tuesday) == []


def test_window_and_trigger_are_correct_in_utc() -> None:
    from datetime import timedelta

    # 2026-04-29 is a Wednesday.
    wed = date(2026, 4, 29)
    ticks = ticks_for_date(SHILSHOLE, wed)
    # Window-start = 18:00 PT = 01:00 UTC next day (during PDT, UTC-7).
    expected_window_start = datetime(2026, 4, 29, 18, 0, tzinfo=PT).astimezone(UTC)
    assert ticks[0].window_start_utc == expected_window_start
    # Lead-12 fires 12 h earlier; lead-0 fires at window_start.
    assert ticks[0].trigger_utc == expected_window_start - timedelta(hours=12)
    assert ticks[-1].trigger_utc == expected_window_start


def test_next_tick_finds_today_when_a_tick_is_in_the_future() -> None:
    # Pretend "now" is 06:00 UTC on Monday 2026-04-27, before the lead-12 tick.
    # Lead-12 fires at 18:00 PT − 12 h = 06:00 PT same day = 13:00 UTC.
    now_utc = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    tick = next_tick(SHILSHOLE, now_utc)
    assert tick is not None
    assert tick.local_date == date(2026, 4, 27)
    assert tick.lead_hours == 12


def test_next_tick_advances_past_already_fired_lead_hours() -> None:
    # Past 13:00 UTC on the race day: lead-12 has fired, lead-8 is next.
    # Lead-8 fires at 18:00 PT − 8 h = 10:00 PT = 17:00 UTC.
    now_utc = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)
    tick = next_tick(SHILSHOLE, now_utc)
    assert tick is not None
    assert tick.lead_hours == 8


def test_next_tick_skips_to_next_race_day_when_today_is_done() -> None:
    # Friday 2026-05-01. Next race day is Monday 2026-05-04.
    now_utc = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    tick = next_tick(SHILSHOLE, now_utc)
    assert tick is not None
    assert tick.local_date == date(2026, 5, 4)
    assert tick.lead_hours == 12


def test_next_tick_returns_none_when_venue_has_no_days() -> None:
    closed_venue = VenueConfig(
        venue_id="closed",
        venue_name="Closed",
        venue_lat=0.0,
        venue_lon=0.0,
        venue_tz="UTC",
        days_of_week=(),
        racing_window_local=(time(18, 0), time(21, 0)),
        lead_hours=(0,),
    )
    assert next_tick(closed_venue, datetime(2026, 4, 27, 0, 0, tzinfo=UTC)) is None


def test_briefing_tick_is_hashable_for_dedupe() -> None:
    """Ticks are dataclasses; same fields hash equal so set-based dedupe works."""
    a = BriefingTick(
        venue_id="x",
        local_date=date(2026, 4, 27),
        lead_hours=12,
        trigger_utc=datetime(2026, 4, 27, 13, 0, tzinfo=UTC),
        window_start_utc=datetime(2026, 4, 28, 1, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 4, 28, 4, 0, tzinfo=UTC),
    )
    b = BriefingTick(
        venue_id="x",
        local_date=date(2026, 4, 27),
        lead_hours=12,
        trigger_utc=datetime(2026, 4, 27, 13, 0, tzinfo=UTC),
        window_start_utc=datetime(2026, 4, 28, 1, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 4, 28, 4, 0, tzinfo=UTC),
    )
    assert {a, b} == {a}
