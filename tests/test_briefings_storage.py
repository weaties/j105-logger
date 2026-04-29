"""Storage round-trip tests for pre-race briefings (#700)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.briefings import (
    Briefing,
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    compose_briefing,
)

if TYPE_CHECKING:
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


def _build_briefing(*, lead_hours: int, local_date: date | None = None) -> Briefing:
    local_date = local_date or date(2026, 4, 29)
    # Window in UTC: 2026-04-30 01:00–04:00 (PT 18:00–21:00 PDT).
    start = datetime(2026, 4, 30, 1, 0, tzinfo=UTC)
    forecast = [
        HourlyForecastSample(
            timestamp_utc=start + timedelta(hours=h),
            wind_speed_kts=10.0 + h,
            wind_gust_kts=14.0 + h,
            wind_direction_deg=200.0 + h,
            air_temp_c=15.0,
            pressure_hpa=1015.0 + h,
            precip_probability_pct=10.0,
            cloud_cover_pct=50.0,
        )
        for h in range(4)
    ]
    tide = [
        HourlyTideSample(
            timestamp_utc=start + timedelta(hours=h),
            tide_height_m=2.0 + 0.1 * h,
            current_speed_kts=0.5 + 0.1 * h,
            current_set_deg=315.0,
        )
        for h in range(4)
    ]
    return compose_briefing(
        venue=SHILSHOLE,
        local_date=local_date,
        lead_hours=lead_hours,
        forecast_samples=forecast,
        tide_samples=tide,
        source_urls={
            "forecast": "https://api.open-meteo.com/v1/forecast?latitude=47.68&longitude=-122.41",
            "tide": "https://tidesandcurrents.noaa.gov/stations.html?id=9447130",
        },
        forecast_issued_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
        tide_station_id="9447130",
        tide_station_name="Seattle, WA",
    )


@pytest.mark.asyncio
async def test_write_and_get_briefing_round_trip(storage: Storage) -> None:
    b = _build_briefing(lead_hours=12)
    briefing_id = await storage.write_briefing(b)
    assert briefing_id > 0

    got = await storage.get_briefing(
        venue_id="shilshole", local_date=date(2026, 4, 29), lead_hours=12
    )
    assert got is not None
    assert got.venue_id == "shilshole"
    assert got.lead_hours == 12
    assert got.state == "Generated"
    assert len(got.hourly_forecast) == 4
    assert len(got.hourly_tide) == 4
    assert got.hourly_forecast[0].wind_speed_kts == 10.0
    assert got.hourly_tide[0].current_set_deg == 315.0
    assert got.pressure_trend == "rising"
    assert got.source_urls["forecast"].startswith("https://api.open-meteo.com")


@pytest.mark.asyncio
async def test_write_briefing_upserts_on_same_triple(storage: Storage) -> None:
    """Repeated writes for the same (venue, local_date, lead_hours) replace."""
    b1 = _build_briefing(lead_hours=12)
    id1 = await storage.write_briefing(b1)
    b2 = _build_briefing(lead_hours=12)
    id2 = await storage.write_briefing(b2)
    # Upsert: same row id is reused, no duplicates.
    assert id1 == id2
    rows = await storage.list_briefings_for_date(venue_id="shilshole", local_date=date(2026, 4, 29))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_list_briefings_for_date_returns_descending_lead(storage: Storage) -> None:
    for lh in (12, 8, 6):
        await storage.write_briefing(_build_briefing(lead_hours=lh))
    rows = await storage.list_briefings_for_date(venue_id="shilshole", local_date=date(2026, 4, 29))
    assert [b.lead_hours for b in rows] == [12, 8, 6]


@pytest.mark.asyncio
async def test_link_briefing_to_race_persists_race_id(storage: Storage) -> None:
    # Need an actual race row to link to.
    race = await storage.start_race(
        event="ShilsholeForecast",
        start_utc=datetime(2026, 4, 30, 1, 0, tzinfo=UTC),
        date_str="2026-04-30",
        race_num=1,
        name="20260429-Shilshole-forecast",
        session_type="forecast",
    )
    b_id = await storage.write_briefing(_build_briefing(lead_hours=12))
    await storage.link_briefing_to_race(briefing_id=b_id, race_id=race.id)
    got = await storage.get_briefing(
        venue_id="shilshole", local_date=date(2026, 4, 29), lead_hours=12
    )
    assert got is not None
    assert got.race_id == race.id

    by_race = await storage.list_briefings_for_race(race.id)
    assert len(by_race) == 1
    assert by_race[0].lead_hours == 12


@pytest.mark.asyncio
async def test_failed_briefing_persists_with_error(storage: Storage) -> None:
    """Failed briefings round-trip with the error message preserved."""
    failed = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=[],
        tide_samples=[],
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        forecast_error="open-meteo timeout",
    )
    await storage.write_briefing(failed)
    got = await storage.get_briefing(
        venue_id="shilshole", local_date=date(2026, 4, 29), lead_hours=12
    )
    assert got is not None
    assert got.state == "Failed"
    assert got.error == "open-meteo timeout"
    assert got.hourly_forecast == ()
