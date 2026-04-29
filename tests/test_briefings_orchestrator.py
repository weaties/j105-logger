"""Orchestrator tests for run_briefing_tick (#700).

The orchestrator is the function that, given a scheduler tick, fetches
the forecast + tide, composes the briefing, persists it, and links/creates
the corresponding Race row. It uses pluggable fetcher callables so we can
test it without the network.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from helmlog.briefings import (
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    run_briefing_tick,
    ticks_for_date,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from helmlog.storage import Storage

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


def _good_forecast_samples(start_utc: datetime) -> list[HourlyForecastSample]:
    return [
        HourlyForecastSample(
            timestamp_utc=start_utc + timedelta(hours=h),
            wind_speed_kts=10.0 + h,
            wind_gust_kts=14.0 + h,
            wind_direction_deg=200.0 + h,
            air_temp_c=15.0,
            pressure_hpa=1015.0,
            precip_probability_pct=10.0,
            cloud_cover_pct=50.0,
        )
        for h in range(4)
    ]


def _good_tide_samples(start_utc: datetime) -> list[HourlyTideSample]:
    return [
        HourlyTideSample(
            timestamp_utc=start_utc + timedelta(hours=h),
            tide_height_m=2.0 + 0.1 * h,
            current_speed_kts=None,
            current_set_deg=None,
        )
        for h in range(4)
    ]


def _make_forecast_fetcher(
    samples: list[HourlyForecastSample], *, raises: bool = False
) -> Callable[..., Coroutine[Any, Any, list[HourlyForecastSample]]]:
    async def fn(
        *, lat: float, lon: float, start_utc: datetime, end_utc: datetime
    ) -> list[HourlyForecastSample]:
        if raises:
            raise RuntimeError("open-meteo 503")
        return samples

    return fn


def _make_tide_fetcher(
    samples: list[HourlyTideSample], *, raises: bool = False
) -> Callable[..., Coroutine[Any, Any, list[HourlyTideSample]]]:
    async def fn(*, lat: float, lon: float, for_date: date) -> list[HourlyTideSample]:
        if raises:
            raise RuntimeError("noaa unreachable")
        return samples

    return fn


@pytest.mark.asyncio
async def test_lead_12_creates_forecast_race_when_none_exists(storage: Storage) -> None:
    tick = ticks_for_date(SHILSHOLE, date(2026, 4, 27))[0]  # lead-12
    assert tick.lead_hours == 12
    forecast = _good_forecast_samples(tick.window_start_utc)
    tide = _good_tide_samples(tick.window_start_utc)

    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
        chart_renderer=None,
        chart_dir=None,
    )
    assert briefing.state == "Generated"
    assert briefing.race_id is not None

    # The race exists, has session_type="forecast", and covers the window.
    race = await storage.get_race(briefing.race_id)
    assert race is not None
    assert race.session_type == "forecast"
    assert race.start_utc == tick.window_start_utc


@pytest.mark.asyncio
async def test_subsequent_lead_links_to_existing_race(storage: Storage) -> None:
    ticks = ticks_for_date(SHILSHOLE, date(2026, 4, 27))
    forecast = _good_forecast_samples(ticks[0].window_start_utc)
    tide = _good_tide_samples(ticks[0].window_start_utc)

    # lead-12 first
    b1 = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=ticks[0],
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
    )
    # lead-8 next
    b2 = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=ticks[1],
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
    )
    assert b2.race_id == b1.race_id
    # Still only ONE race row (the forecast one), not two.
    races = await storage.list_races_for_date("2026-04-28")
    # Wait — race date is the UTC date of start_utc (2026-04-28 01:00 UTC).
    assert len(races) == 1
    assert races[0].id == b1.race_id


@pytest.mark.asyncio
async def test_existing_race_is_reused_regardless_of_session_type(storage: Storage) -> None:
    """If a real Race already covers the window, link to it instead of creating."""
    ticks = ticks_for_date(SHILSHOLE, date(2026, 4, 27))
    tick = ticks[0]
    real_race = await storage.start_race(
        event="BallardCup",
        start_utc=tick.window_start_utc,
        date_str=tick.window_start_utc.date().isoformat(),
        race_num=1,
        name="20260427-BallardCup-1",
        session_type="race",
    )
    forecast = _good_forecast_samples(tick.window_start_utc)
    tide = _good_tide_samples(tick.window_start_utc)
    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
    )
    assert briefing.race_id == real_race.id
    refetched = await storage.get_race(real_race.id)
    assert refetched is not None
    assert refetched.session_type == "race"  # unchanged


@pytest.mark.asyncio
async def test_forecast_fetch_failure_persists_failed_state_no_race_created(
    storage: Storage,
) -> None:
    tick = ticks_for_date(SHILSHOLE, date(2026, 4, 27))[0]
    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher([], raises=True),
        fetch_tide=_make_tide_fetcher(_good_tide_samples(tick.window_start_utc)),
    )
    assert briefing.state == "Failed"
    assert briefing.error is not None
    assert "open-meteo" in briefing.error.lower()
    assert briefing.race_id is None
    races = await storage.list_races_for_date(tick.window_start_utc.date().isoformat())
    assert races == []


@pytest.mark.asyncio
async def test_tide_failure_keeps_briefing_generated_with_reason(storage: Storage) -> None:
    tick = ticks_for_date(SHILSHOLE, date(2026, 4, 27))[0]
    forecast = _good_forecast_samples(tick.window_start_utc)
    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher([], raises=True),
    )
    assert briefing.state == "Generated"
    assert briefing.hourly_tide == ()
    assert briefing.tide_unavailable_reason is not None
    assert "noaa" in briefing.tide_unavailable_reason.lower()


@pytest.mark.asyncio
async def test_chart_renderer_is_called_best_effort(storage: Storage, tmp_path: Path) -> None:
    tick = ticks_for_date(SHILSHOLE, date(2026, 4, 27))[0]
    forecast = _good_forecast_samples(tick.window_start_utc)
    tide = _good_tide_samples(tick.window_start_utc)
    calls: list[Path] = []

    def renderer(briefing: object, path: Path) -> bool:
        path.write_bytes(b"\x89PNG fake")
        calls.append(path)
        return True

    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
        chart_renderer=renderer,
        chart_dir=tmp_path,
    )
    assert briefing.state == "Generated"
    assert briefing.chart_path is not None
    assert calls
    assert calls[0].exists()


@pytest.mark.asyncio
async def test_chart_renderer_failure_does_not_block_briefing(
    storage: Storage, tmp_path: Path
) -> None:
    tick = ticks_for_date(SHILSHOLE, date(2026, 4, 27))[0]
    forecast = _good_forecast_samples(tick.window_start_utc)
    tide = _good_tide_samples(tick.window_start_utc)

    def renderer(briefing: object, path: Path) -> bool:
        raise RuntimeError("matplotlib boom")

    briefing = await run_briefing_tick(
        storage=storage,
        venue=SHILSHOLE,
        tick=tick,
        fetch_forecast=_make_forecast_fetcher(forecast),
        fetch_tide=_make_tide_fetcher(tide),
        chart_renderer=renderer,
        chart_dir=tmp_path,
    )
    assert briefing.state == "Generated"
    assert briefing.chart_path is None
