"""Tests for compose_briefing (#700) — pure briefing assembly."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from helmlog.briefings import (
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    compose_briefing,
)

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


def _wed_window_utc() -> tuple[datetime, datetime]:
    # 2026-04-29 18:00–21:00 PT (PDT, UTC-7) → 01:00–04:00 UTC on 2026-04-30.
    return (
        datetime(2026, 4, 30, 1, 0, tzinfo=UTC),
        datetime(2026, 4, 30, 4, 0, tzinfo=UTC),
    )


def _hourly_forecast(
    *,
    pressures: list[float] | None = None,
) -> list[HourlyForecastSample]:
    """Build hourly forecast samples covering the racing window plus padding."""
    start, _end = _wed_window_utc()
    pressures = pressures or [1015, 1015, 1015, 1015]
    samples: list[HourlyForecastSample] = []
    # Include one sample before and after the window so window-filtering is
    # actually exercised.
    for offset, p in enumerate([1014.0, *pressures, 1014.0]):
        ts = start.replace() + (offset - 1) * (start - start)  # placeholder
        from datetime import timedelta

        ts = start + timedelta(hours=offset - 1)
        samples.append(
            HourlyForecastSample(
                timestamp_utc=ts,
                wind_speed_kts=10.0 + offset,
                wind_gust_kts=14.0 + offset,
                wind_direction_deg=200.0 + offset,
                air_temp_c=15.0,
                pressure_hpa=p,
                precip_probability_pct=10.0,
                cloud_cover_pct=50.0,
            )
        )
    return samples


def _hourly_tide() -> list[HourlyTideSample]:
    start, end = _wed_window_utc()
    from datetime import timedelta

    samples: list[HourlyTideSample] = []
    n_hours = int((end - start).total_seconds() // 3600) + 1
    for h in range(n_hours):
        samples.append(
            HourlyTideSample(
                timestamp_utc=start + timedelta(hours=h),
                tide_height_m=2.0 + 0.1 * h,
                current_speed_kts=0.5 + 0.1 * h,
                current_set_deg=315.0,
            )
        )
    return samples


def test_generated_state_with_full_data() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=_hourly_forecast(pressures=[1015, 1015, 1015, 1015]),
        tide_samples=_hourly_tide(),
        source_urls={
            "forecast": "https://api.open-meteo.com/...",
            "tide": "https://tidesandcurrents.noaa.gov/stations.html?id=9447130",
        },
        forecast_issued_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
    )
    assert b.state == "Generated"
    assert b.lead_hours == 12
    # Window covers exactly 4 hour-boundaries (18:00, 19:00, 20:00, 21:00 PT).
    assert len(b.hourly_forecast) == 4
    assert len(b.hourly_tide) == 4
    assert b.error is None
    assert b.source_urls["forecast"].startswith("https://")


def test_pressure_trend_rising() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=8,
        forecast_samples=_hourly_forecast(pressures=[1010, 1012, 1014, 1016]),
        tide_samples=_hourly_tide(),
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
    )
    assert b.state == "Generated"
    assert b.pressure_trend == "rising"


def test_pressure_trend_falling() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=8,
        forecast_samples=_hourly_forecast(pressures=[1020, 1018, 1015, 1012]),
        tide_samples=_hourly_tide(),
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
    )
    assert b.pressure_trend == "falling"


def test_pressure_trend_steady_when_delta_is_within_threshold() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=8,
        forecast_samples=_hourly_forecast(pressures=[1015.0, 1015.4, 1015.2, 1015.5]),
        tide_samples=_hourly_tide(),
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
    )
    assert b.pressure_trend == "steady"


def test_no_forecast_samples_yields_failed_state() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=[],
        tide_samples=_hourly_tide(),
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        forecast_error="open-meteo 503",
    )
    assert b.state == "Failed"
    assert b.error == "open-meteo 503"
    assert b.hourly_forecast == ()
    assert b.hourly_tide == ()


def test_tide_missing_keeps_briefing_generated_with_reason() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=_hourly_forecast(),
        tide_samples=[],
        source_urls={"forecast": "https://api.open-meteo.com/..."},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        tide_error="no NOAA station within 50 km",
    )
    assert b.state == "Generated"
    assert b.hourly_tide == ()
    assert b.tide_unavailable_reason == "no NOAA station within 50 km"


def test_forecast_window_filtering_drops_padding_samples() -> None:
    b = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=6,
        forecast_samples=_hourly_forecast(),  # has 1 pre + 4 in-window + 1 post
        tide_samples=_hourly_tide(),
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
    )
    assert len(b.hourly_forecast) == 4
    start, end = _wed_window_utc()
    for s in b.hourly_forecast:
        assert start <= s.timestamp_utc <= end
