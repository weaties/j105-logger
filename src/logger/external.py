"""External data sources — tides, weather, etc.

Uses httpx.AsyncClient for all HTTP requests. ExternalFetcher must be used as
an async context manager to manage the shared HTTP client lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TideReading:
    """A single tide height observation or prediction."""

    timestamp: datetime
    height_m: float  # metres above chart datum
    type: str  # "prediction" | "observation"


@dataclass(frozen=True)
class WeatherReading:
    """A single weather observation from Open-Meteo (hourly resolution)."""

    timestamp: datetime  # UTC time of the reading (truncated to hour)
    lat: float  # latitude used for the query
    lon: float  # longitude used for the query
    wind_speed_kts: float  # 10 m wind speed in knots
    wind_direction_deg: float  # 10 m wind direction (degrees true, 0 = N)
    air_temp_c: float  # 2 m air temperature (°C)
    pressure_hpa: float  # surface pressure (hPa)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class ExternalFetcher:
    """Fetches external environmental data from web APIs."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ExternalFetcher:
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ExternalFetcher must be used as an async context manager")
        return self._client

    async def fetch_tides(
        self,
        lat: float,
        lon: float,
        dt: datetime,
    ) -> TideReading | None:
        """Fetch tide height for the given location and time.

        TODO: Integrate with a real tide API, e.g.:
            - NOAA CO-OPS (tidesandcurrents.noaa.gov/api/)
            - UK NTSLF (ntslf.org)
            - WorldTides (worldtides.info/api)

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.
            dt:  UTC datetime for the tide reading.

        Returns:
            A TideReading, or None if the request fails.
        """
        logger.debug("fetch_tides: lat={} lon={} dt={} (stub)", lat, lon, dt)
        # TODO: replace with real API call
        return None

    async def fetch_weather(
        self,
        lat: float,
        lon: float,
        dt: datetime,
    ) -> WeatherReading | None:
        """Fetch current weather from Open-Meteo for the given location.

        Calls the /v1/forecast endpoint with the `current` parameter to get
        wind speed (kts), wind direction, air temperature, and surface pressure
        for the current UTC hour.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.
            dt:  UTC datetime (used only for logging; Open-Meteo returns the
                 current hour regardless).

        Returns:
            A WeatherReading, or None if the request fails or the response
            cannot be parsed.
        """
        logger.debug("fetch_weather: lat={:.4f} lon={:.4f} dt={}", lat, lon, dt)

        params: dict[str, Any] = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "current": "wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure",
            "wind_speed_unit": "kn",
            "timezone": "UTC",
        }
        try:
            resp = await self._http().get(_OPEN_METEO_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Weather fetch failed: {}", exc)
            return None

        try:
            data: dict[str, Any] = resp.json()
            current = data["current"]
            from datetime import UTC as _UTC
            from datetime import datetime as _datetime

            ts = _datetime.fromisoformat(current["time"]).replace(tzinfo=_UTC)
            reading = WeatherReading(
                timestamp=ts,
                lat=lat,
                lon=lon,
                wind_speed_kts=float(current["wind_speed_10m"]),
                wind_direction_deg=float(current["wind_direction_10m"]),
                air_temp_c=float(current["temperature_2m"]),
                pressure_hpa=float(current["surface_pressure"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Weather response parse error: {}", exc)
            return None

        logger.info(
            "Weather: {:.1f} kts from {:.0f}°, {:.1f}°C, {:.0f} hPa",
            reading.wind_speed_kts,
            reading.wind_direction_deg,
            reading.air_temp_c,
            reading.pressure_hpa,
        )
        return reading
