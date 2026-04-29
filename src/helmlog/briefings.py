"""Pre-race weather briefings (#700).

A briefing is a snapshot of forecast wind, weather, tide, and current for a
configured venue's racing window. The job runs at multiple lead times
(default 12, 8, 6, 4, 2, 0 hours before window-start) and persists each run
so the trend across forecasts is preserved for debrief.

This module owns:

- ``VenueConfig`` and the venue registry (Shilshole seeded; others added by
  config).
- ``Briefing`` dataclass and the pure ``compose_briefing`` function that
  turns hourly forecast + tide samples into a stored record.
- Scheduler tick computation: given a venue and "now" in venue-local time,
  what is the next ``(local_date, lead_hours)`` triple to run?

Storage is in ``storage.py`` and the chart renderer / web routes live in
their own modules. Hardware and HTTP I/O are kept out of this file so the
logic is testable without a network or a Pi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from helmlog.storage import Storage


@dataclass(frozen=True)
class VenueConfig:
    """Per-venue configuration for the pre-race briefing job.

    All scheduling fields are interpreted in ``venue_tz``. ``days_of_week``
    uses Python's ``date.weekday()`` convention (Monday=0). ``lead_hours``
    is the list of hours before ``racing_window_local[0]`` at which a
    briefing should be generated; the largest lead fires first.
    """

    venue_id: str
    venue_name: str
    venue_lat: float
    venue_lon: float
    venue_tz: str
    days_of_week: tuple[int, ...]
    racing_window_local: tuple[time, time]
    lead_hours: tuple[int, ...]

    def __init__(
        self,
        *,
        venue_id: str,
        venue_name: str,
        venue_lat: float,
        venue_lon: float,
        venue_tz: str,
        days_of_week: Iterable[int],
        racing_window_local: tuple[time, time],
        lead_hours: Iterable[int],
    ) -> None:
        # Custom __init__ so callers can pass lists; the stored values are
        # tuples (frozen dataclass equality + hashability).
        object.__setattr__(self, "venue_id", venue_id)
        object.__setattr__(self, "venue_name", venue_name)
        object.__setattr__(self, "venue_lat", venue_lat)
        object.__setattr__(self, "venue_lon", venue_lon)
        object.__setattr__(self, "venue_tz", venue_tz)
        object.__setattr__(self, "days_of_week", tuple(days_of_week))
        object.__setattr__(self, "racing_window_local", racing_window_local)
        # Sort descending so the earliest lead (e.g. 12 h) runs first.
        object.__setattr__(self, "lead_hours", tuple(sorted(lead_hours, reverse=True)))


# ---------------------------------------------------------------------------
# Venue registry
# ---------------------------------------------------------------------------

_SHILSHOLE = VenueConfig(
    venue_id="shilshole",
    venue_name="Shilshole Bay",
    venue_lat=47.6800,
    venue_lon=-122.4067,
    venue_tz="America/Los_Angeles",
    days_of_week=(0, 2),  # Monday, Wednesday
    racing_window_local=(time(18, 0), time(21, 0)),
    lead_hours=(12, 8, 6, 4, 2, 0),
)

_REGISTRY: dict[str, VenueConfig] = {
    _SHILSHOLE.venue_id: _SHILSHOLE,
}


def get_venue(venue_id: str) -> VenueConfig | None:
    """Look up a venue by id. Returns None if not registered."""
    return _REGISTRY.get(venue_id)


def list_venues() -> list[VenueConfig]:
    """Return all registered venues."""
    return list(_REGISTRY.values())


def register_venue(venue: VenueConfig) -> None:
    """Register a venue (used by config loaders / tests).

    Replaces any prior registration with the same ``venue_id``.
    """
    _REGISTRY[venue.venue_id] = venue


# ---------------------------------------------------------------------------
# Scheduler tick computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefingTick:
    """A single (venue, local_date, lead_hours) trigger that should run.

    ``trigger_utc`` is the UTC moment the tick fires. The composer uses
    ``window_start_utc``/``window_end_utc`` for the forecast slice it pulls.
    """

    venue_id: str
    local_date: date
    lead_hours: int
    trigger_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime


def _local_window_to_utc(venue: VenueConfig, local_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(venue.venue_tz)
    start_local = datetime.combine(local_date, venue.racing_window_local[0], tzinfo=tz)
    end_local = datetime.combine(local_date, venue.racing_window_local[1], tzinfo=tz)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def ticks_for_date(venue: VenueConfig, local_date: date) -> list[BriefingTick]:
    """Return all ticks for a specific venue-local date.

    If ``local_date`` is not one of the venue's ``days_of_week``, returns
    ``[]``. Otherwise returns one tick per entry in ``lead_hours``, in
    descending lead order (so the earliest fire is first).
    """
    if local_date.weekday() not in venue.days_of_week:
        return []
    start_utc, end_utc = _local_window_to_utc(venue, local_date)
    return [
        BriefingTick(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lh,
            trigger_utc=start_utc - timedelta(hours=lh),
            window_start_utc=start_utc,
            window_end_utc=end_utc,
        )
        for lh in venue.lead_hours
    ]


def next_tick(venue: VenueConfig, now_utc: datetime) -> BriefingTick | None:
    """Return the next tick at or after ``now_utc`` for this venue.

    Walks forward up to 8 days to find the next race day; returns None if
    the venue has no configured days (defensive).
    """
    if not venue.days_of_week:
        return None
    tz = ZoneInfo(venue.venue_tz)
    today_local = now_utc.astimezone(tz).date()
    for offset in range(8):
        candidate = today_local + timedelta(days=offset)
        for tick in ticks_for_date(venue, candidate):
            if tick.trigger_utc >= now_utc:
                return tick
    return None


# ---------------------------------------------------------------------------
# Briefing dataclass + composer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HourlyForecastSample:
    """One hour of weather forecast at the venue."""

    timestamp_utc: datetime
    wind_speed_kts: float
    wind_gust_kts: float
    wind_direction_deg: float
    air_temp_c: float
    pressure_hpa: float
    precip_probability_pct: float
    cloud_cover_pct: float


@dataclass(frozen=True)
class HourlyTideSample:
    """One hour of tide height + current at the venue."""

    timestamp_utc: datetime
    tide_height_m: float | None
    current_speed_kts: float | None
    current_set_deg: float | None  # direction current is flowing toward


@dataclass(frozen=True)
class Briefing:
    """A composed pre-race briefing for one (venue, local_date, lead_hours)."""

    venue_id: str
    local_date: date
    lead_hours: int
    state: str  # "Generated" | "Failed"
    hourly_forecast: tuple[HourlyForecastSample, ...]
    hourly_tide: tuple[HourlyTideSample, ...]
    pressure_trend: str  # "rising" | "steady" | "falling" | "unknown"
    source_urls: dict[str, str] = field(default_factory=dict)
    forecast_issued_at: datetime | None = None
    fetched_at: datetime | None = None
    error: str | None = None
    tide_unavailable_reason: str | None = None
    tide_station_id: str | None = None
    tide_station_name: str | None = None
    chart_path: str | None = None
    race_id: int | None = None


_PRESSURE_STEADY_HPA = 1.0  # |Δ| ≤ 1 hPa across the racing window → "steady"


def _pressure_trend(samples: Sequence[HourlyForecastSample]) -> str:
    if len(samples) < 2:
        return "unknown"
    delta = samples[-1].pressure_hpa - samples[0].pressure_hpa
    if abs(delta) <= _PRESSURE_STEADY_HPA:
        return "steady"
    return "rising" if delta > 0 else "falling"


def compose_briefing(
    *,
    venue: VenueConfig,
    local_date: date,
    lead_hours: int,
    forecast_samples: Sequence[HourlyForecastSample],
    tide_samples: Sequence[HourlyTideSample],
    source_urls: dict[str, str],
    forecast_issued_at: datetime | None,
    fetched_at: datetime,
    forecast_error: str | None = None,
    tide_error: str | None = None,
    tide_station_id: str | None = None,
    tide_station_name: str | None = None,
) -> Briefing:
    """Compose a Briefing from already-fetched forecast and tide data.

    Pure function — no I/O. Fail-safes:

    - If ``forecast_samples`` is empty (or ``forecast_error`` is set with
      no samples), the briefing is returned in ``Failed`` state with the
      error message attached. Tide data is dropped.
    - If ``tide_samples`` is empty but forecast samples are present, the
      briefing is returned in ``Generated`` state with an empty tide
      block and ``tide_unavailable_reason`` populated.
    - The samples are filtered to the racing window (inclusive of both
      ends) and sorted by timestamp before storage.
    """
    window_start_utc, window_end_utc = _local_window_to_utc(venue, local_date)

    if not forecast_samples:
        return Briefing(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lead_hours,
            state="Failed",
            hourly_forecast=(),
            hourly_tide=(),
            pressure_trend="unknown",
            source_urls=dict(source_urls),
            forecast_issued_at=forecast_issued_at,
            fetched_at=fetched_at,
            error=forecast_error or "no forecast samples",
        )

    forecast_in_window = tuple(
        sorted(
            (s for s in forecast_samples if window_start_utc <= s.timestamp_utc <= window_end_utc),
            key=lambda s: s.timestamp_utc,
        )
    )

    if not forecast_in_window:
        return Briefing(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lead_hours,
            state="Failed",
            hourly_forecast=(),
            hourly_tide=(),
            pressure_trend="unknown",
            source_urls=dict(source_urls),
            forecast_issued_at=forecast_issued_at,
            fetched_at=fetched_at,
            error="no forecast samples covered the racing window",
        )

    tide_in_window = tuple(
        sorted(
            (s for s in tide_samples if window_start_utc <= s.timestamp_utc <= window_end_utc),
            key=lambda s: s.timestamp_utc,
        )
    )

    return Briefing(
        venue_id=venue.venue_id,
        local_date=local_date,
        lead_hours=lead_hours,
        state="Generated",
        hourly_forecast=forecast_in_window,
        hourly_tide=tide_in_window,
        pressure_trend=_pressure_trend(forecast_in_window),
        source_urls=dict(source_urls),
        forecast_issued_at=forecast_issued_at,
        fetched_at=fetched_at,
        error=None,
        tide_unavailable_reason=tide_error if not tide_in_window else None,
        tide_station_id=tide_station_id,
        tide_station_name=tide_station_name,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_NOAA_STATION_BASE_URL = "https://tidesandcurrents.noaa.gov/stationhome.html?id="


class _ForecastFetcher(Protocol):
    async def __call__(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Sequence[HourlyForecastSample]: ...


class _TideFetcher(Protocol):
    async def __call__(
        self,
        *,
        lat: float,
        lon: float,
        for_date: date,
    ) -> Sequence[Any]: ...


def _forecast_url(venue: VenueConfig) -> str:
    return (
        f"{_OPEN_METEO_BASE_URL}?latitude={venue.venue_lat:.2f}"
        f"&longitude={venue.venue_lon:.2f}&hourly=wind_speed_10m,wind_direction_10m,"
        "wind_gusts_10m,temperature_2m,precipitation_probability,cloud_cover,surface_pressure"
    )


def _tide_to_hourly(tide_readings: Sequence[Any]) -> tuple[list[HourlyTideSample], str | None]:
    """Adapt TideReading-like objects to HourlyTideSample. Currents come later."""
    samples: list[HourlyTideSample] = []
    station_id: str | None = None
    for r in tide_readings:
        ts = getattr(r, "timestamp", None)
        height = getattr(r, "height_m", None)
        if ts is None or height is None:
            continue
        if station_id is None:
            station_id = getattr(r, "station_id", None)
        samples.append(
            HourlyTideSample(
                timestamp_utc=ts,
                tide_height_m=float(height),
                current_speed_kts=None,
                current_set_deg=None,
            )
        )
    return samples, station_id


async def run_briefing_tick(
    *,
    storage: Storage,
    venue: VenueConfig,
    tick: BriefingTick,
    fetch_forecast: _ForecastFetcher,
    fetch_tide: _TideFetcher,
    chart_renderer: Callable[[Briefing, Path], bool] | None = None,
    chart_dir: Path | None = None,
    now_utc: datetime | None = None,
) -> Briefing:
    """Execute a single scheduler tick: fetch, compose, persist, link Race.

    Pure-domain composition is delegated to ``compose_briefing``. This
    function owns the side effects: network fetches via the injected
    callables, DB writes via ``storage``, optional chart rendering.

    The tide source error is captured (never raised) so a tide outage
    doesn't fail the whole briefing — matches the spec's fail-safe rules.
    A forecast outage produces a ``Failed`` briefing and skips Race
    auto-creation.
    """
    fetched_at = now_utc or datetime.now(UTC)

    forecast_samples: list[HourlyForecastSample] = []
    forecast_error: str | None = None
    try:
        forecast_samples = list(
            await fetch_forecast(
                lat=venue.venue_lat,
                lon=venue.venue_lon,
                start_utc=tick.window_start_utc,
                end_utc=tick.window_end_utc,
            )
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: capture and continue
        forecast_error = str(exc)
        logger.warning(
            "briefing forecast fetch failed venue={} date={} lead={}h err={}",
            venue.venue_id,
            tick.local_date,
            tick.lead_hours,
            exc,
        )

    tide_samples: list[HourlyTideSample] = []
    tide_error: str | None = None
    tide_station_id: str | None = None
    tide_station_name: str | None = None
    try:
        tide_readings = await fetch_tide(
            lat=venue.venue_lat,
            lon=venue.venue_lon,
            for_date=tick.window_start_utc.date(),
        )
        tide_samples, tide_station_id = _tide_to_hourly(tide_readings)
        if tide_readings:
            tide_station_name = getattr(tide_readings[0], "station_name", None)
    except Exception as exc:  # noqa: BLE001
        tide_error = str(exc)
        logger.warning(
            "briefing tide fetch failed venue={} date={} err={}",
            venue.venue_id,
            tick.local_date,
            exc,
        )

    source_urls: dict[str, str] = {"forecast": _forecast_url(venue)}
    if tide_station_id:
        source_urls["tide"] = f"{_NOAA_STATION_BASE_URL}{tide_station_id}"

    briefing = compose_briefing(
        venue=venue,
        local_date=tick.local_date,
        lead_hours=tick.lead_hours,
        forecast_samples=forecast_samples,
        tide_samples=tide_samples,
        source_urls=source_urls,
        forecast_issued_at=None,
        fetched_at=fetched_at,
        forecast_error=forecast_error,
        tide_error=tide_error,
        tide_station_id=tide_station_id,
        tide_station_name=tide_station_name,
    )

    # Race linking — only when the forecast succeeded. A Failed briefing
    # never auto-creates a Race row.
    race_id: int | None = None
    if briefing.state == "Generated":
        race_id = await _link_or_create_race(storage, venue, tick)
        briefing = _briefing_with_race_id(briefing, race_id)

    # Best-effort chart render. Path is set on the briefing only on success.
    if chart_renderer is not None and chart_dir is not None and briefing.state == "Generated":
        chart_dir.mkdir(parents=True, exist_ok=True)
        chart_filename = (
            f"{venue.venue_id}_{tick.local_date.isoformat()}_l{tick.lead_hours:02d}.png"
        )
        chart_path = chart_dir / chart_filename
        try:
            ok = chart_renderer(briefing, chart_path)
            if ok and chart_path.exists():
                briefing = _briefing_with_chart_path(briefing, str(chart_path))
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "briefing chart render failed venue={} lead={}h err={}",
                venue.venue_id,
                tick.lead_hours,
                exc,
            )

    briefing_id = await storage.write_briefing(briefing)
    if race_id is not None:
        await storage.link_briefing_to_race(briefing_id=briefing_id, race_id=race_id)

    logger.info(
        "briefing {} venue={} date={} lead={}h state={} race={}",
        briefing_id,
        venue.venue_id,
        tick.local_date.isoformat(),
        tick.lead_hours,
        briefing.state,
        race_id,
    )
    return briefing


async def _link_or_create_race(storage: Storage, venue: VenueConfig, tick: BriefingTick) -> int:
    """Find an existing Race covering the racing window, or create a forecast one."""
    from helmlog.races import build_race_name

    existing = await storage.list_races_in_range(tick.window_start_utc, tick.window_end_utc)
    if existing:
        # Pick the first race whose start time matches the window most closely.
        return existing[0].id

    date_str = tick.window_start_utc.date().isoformat()
    same_day = await storage.list_races_for_date(date_str)
    race_num = sum(1 for r in same_day if r.session_type == "forecast") + 1
    name = build_race_name(
        event=venue.venue_name.replace(" ", ""),
        d=tick.local_date,
        race_num=race_num,
        session_type="forecast",
    )
    race = await storage.start_race(
        event=venue.venue_name.replace(" ", ""),
        start_utc=tick.window_start_utc,
        date_str=date_str,
        race_num=race_num,
        name=name,
        session_type="forecast",
    )
    # The forecast race is a placeholder — close it immediately so the
    # session_active flag doesn't trip elsewhere.
    await storage.end_race(race.id, tick.window_end_utc)
    return race.id


def _briefing_with_race_id(b: Briefing, race_id: int) -> Briefing:
    return Briefing(
        venue_id=b.venue_id,
        local_date=b.local_date,
        lead_hours=b.lead_hours,
        state=b.state,
        hourly_forecast=b.hourly_forecast,
        hourly_tide=b.hourly_tide,
        pressure_trend=b.pressure_trend,
        source_urls=dict(b.source_urls),
        forecast_issued_at=b.forecast_issued_at,
        fetched_at=b.fetched_at,
        error=b.error,
        tide_unavailable_reason=b.tide_unavailable_reason,
        tide_station_id=b.tide_station_id,
        tide_station_name=b.tide_station_name,
        chart_path=b.chart_path,
        race_id=race_id,
    )


# ---------------------------------------------------------------------------
# Chart renderer (matplotlib)
# ---------------------------------------------------------------------------


_CHART_WIDTH_PX = 1200
_CHART_HEIGHT_PX = 630
_CHART_DPI = 100


def render_chart(briefing: Briefing, output_path: Path) -> bool:
    """Render a 1200x630 PNG showing wind + current across the racing window.

    The chart has two stacked rows:

    - **Top:** wind barbs at each hour, a wind-speed line with the gust
      band shaded, and the wind-direction labelled per hour.
    - **Bottom:** tide height curve with current arrows (kts + set deg)
      where available. If no tide data exists the row collapses to a
      "tide unavailable" annotation.

    Returns True on success. The renderer is best-effort: callers should
    treat a False return (or an exception caught upstream) as "chart
    unavailable" without failing the whole briefing.
    """
    # Lazy import — keeps `helmlog.briefings` cheap to import in code paths
    # that don't render charts (Storage, web detail pages without a chart).
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    if not briefing.hourly_forecast:
        return False

    fig, (ax_wind, ax_tide) = plt.subplots(
        2,
        1,
        figsize=(_CHART_WIDTH_PX / _CHART_DPI, _CHART_HEIGHT_PX / _CHART_DPI),
        dpi=_CHART_DPI,
        gridspec_kw={"height_ratios": [3, 2]},
    )

    times = [s.timestamp_utc for s in briefing.hourly_forecast]
    speeds = [s.wind_speed_kts for s in briefing.hourly_forecast]
    gusts = [s.wind_gust_kts for s in briefing.hourly_forecast]
    dirs = [s.wind_direction_deg for s in briefing.hourly_forecast]

    ax_wind.fill_between(times, speeds, gusts, alpha=0.25, color="#1f77b4", label="gust")
    ax_wind.plot(times, speeds, "o-", color="#1f77b4", label="wind (kts)")
    ax_wind.set_ylabel("wind (kts)")
    ax_wind.set_title(
        f"{_venue_display_name(briefing.venue_id)} — "
        f"{briefing.local_date.isoformat()} (lead {briefing.lead_hours} h)"
    )
    ax_wind.grid(True, alpha=0.3)
    for t, d in zip(times, dirs, strict=False):
        ax_wind.annotate(
            f"{int(round(d))}°",
            xy=(t, ax_wind.get_ylim()[1]),
            xytext=(0, -10),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#444",
        )
    ax_wind.legend(loc="upper left", fontsize=8)

    if briefing.hourly_tide:
        tide_times = [s.timestamp_utc for s in briefing.hourly_tide]
        heights = [
            s.tide_height_m if s.tide_height_m is not None else 0.0 for s in briefing.hourly_tide
        ]
        ax_tide.plot(tide_times, heights, "o-", color="#2ca02c", label="tide (m)")
        ax_tide.set_ylabel("tide (m, MLLW)")
        ax_tide.grid(True, alpha=0.3)
        # Current arrows: only where speed and direction are present.
        for s in briefing.hourly_tide:
            if s.current_speed_kts is None or s.current_set_deg is None:
                continue
            import math

            dx = math.sin(math.radians(s.current_set_deg)) * s.current_speed_kts
            dy = math.cos(math.radians(s.current_set_deg)) * s.current_speed_kts
            ax_tide.annotate(
                f"{s.current_speed_kts:.1f} kt @ {int(round(s.current_set_deg))}°",
                xy=(s.timestamp_utc, s.tide_height_m or 0.0),
                xytext=(8, 8),
                textcoords="offset points",
                fontsize=7,
                color="#666",
                arrowprops={"arrowstyle": "->", "color": "#888", "lw": 0.6},
            )
            _ = dx, dy  # values currently used only for the offset hint
        ax_tide.legend(loc="upper left", fontsize=8)
    else:
        ax_tide.text(
            0.5,
            0.5,
            briefing.tide_unavailable_reason or "tide unavailable",
            ha="center",
            va="center",
            transform=ax_tide.transAxes,
            color="#888",
        )
        ax_tide.set_axis_off()

    fig.autofmt_xdate()
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=_CHART_DPI, format="png")
    plt.close(fig)
    return output_path.exists() and output_path.stat().st_size > 0


def _venue_display_name(venue_id: str) -> str:
    venue = get_venue(venue_id)
    return venue.venue_name if venue is not None else venue_id


def _briefing_with_chart_path(b: Briefing, chart_path: str) -> Briefing:
    return Briefing(
        venue_id=b.venue_id,
        local_date=b.local_date,
        lead_hours=b.lead_hours,
        state=b.state,
        hourly_forecast=b.hourly_forecast,
        hourly_tide=b.hourly_tide,
        pressure_trend=b.pressure_trend,
        source_urls=dict(b.source_urls),
        forecast_issued_at=b.forecast_issued_at,
        fetched_at=b.fetched_at,
        error=b.error,
        tide_unavailable_reason=b.tide_unavailable_reason,
        tide_station_id=b.tide_station_id,
        tide_station_name=b.tide_station_name,
        chart_path=chart_path,
        race_id=b.race_id,
    )
