"""External data sources — tides, weather, etc.

Real API endpoints are TBD. Stub implementations are provided with clear TODO
markers. Uses httpx.AsyncClient for all HTTP requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    """A single weather observation or forecast."""

    timestamp: datetime
    wind_speed_kts: float
    wind_direction_deg: float
    air_temp_c: float
    pressure_hpa: float


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
        """Fetch weather observation/forecast for the given location and time.

        TODO: Integrate with a real weather API, e.g.:
            - Open-Meteo (open-meteo.com) — free, no key required
            - OpenWeatherMap (openweathermap.org/api)
            - NOAA NWS (api.weather.gov)

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.
            dt:  UTC datetime for the weather reading.

        Returns:
            A WeatherReading, or None if the request fails.
        """
        logger.debug("fetch_weather: lat={} lon={} dt={} (stub)", lat, lon, dt)
        # TODO: replace with real API call
        _ = self._http()  # ensure client is ready when implemented
        return None
