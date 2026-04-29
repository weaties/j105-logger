"""Smoke tests for the briefing chart renderer (#700).

The renderer is matplotlib-backed but invoked through ``render_chart``
which writes a PNG to disk. We assert the file exists, has non-trivial
size, and starts with a PNG magic-number — without parsing pixel data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from helmlog.briefings import (
    Briefing,
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    compose_briefing,
    render_chart,
)

if TYPE_CHECKING:
    from pathlib import Path

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
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _briefing(*, with_tide: bool, with_currents: bool) -> Briefing:
    start = datetime(2026, 4, 30, 1, 0, tzinfo=UTC)
    forecast = [
        HourlyForecastSample(
            timestamp_utc=start + timedelta(hours=h),
            wind_speed_kts=8.0 + h,
            wind_gust_kts=12.0 + h,
            wind_direction_deg=200.0 + 5 * h,
            air_temp_c=15.0,
            pressure_hpa=1015.0 + 0.5 * h,
            precip_probability_pct=10.0,
            cloud_cover_pct=50.0,
        )
        for h in range(4)
    ]
    tide: list[HourlyTideSample] = []
    if with_tide:
        tide = [
            HourlyTideSample(
                timestamp_utc=start + timedelta(hours=h),
                tide_height_m=2.0 + 0.1 * h,
                current_speed_kts=(0.5 + 0.1 * h) if with_currents else None,
                current_set_deg=315.0 if with_currents else None,
            )
            for h in range(4)
        ]
    return compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=forecast,
        tide_samples=tide,
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        tide_error="" if with_tide else "no NOAA station",
    )


def test_render_chart_writes_a_png_with_full_data(tmp_path: Path) -> None:
    out = tmp_path / "shilshole.png"
    ok = render_chart(_briefing(with_tide=True, with_currents=True), out)
    assert ok is True
    assert out.exists()
    assert out.stat().st_size > 1000
    assert out.read_bytes()[:8] == PNG_MAGIC


def test_render_chart_works_without_currents(tmp_path: Path) -> None:
    """Tide heights present, currents absent — chart still renders."""
    out = tmp_path / "no-currents.png"
    ok = render_chart(_briefing(with_tide=True, with_currents=False), out)
    assert ok is True
    assert out.exists()
    assert out.read_bytes()[:8] == PNG_MAGIC


def test_render_chart_works_with_wind_only(tmp_path: Path) -> None:
    """Tide unavailable — wind-only chart still renders."""
    out = tmp_path / "wind-only.png"
    ok = render_chart(_briefing(with_tide=False, with_currents=False), out)
    assert ok is True
    assert out.exists()
    assert out.read_bytes()[:8] == PNG_MAGIC
