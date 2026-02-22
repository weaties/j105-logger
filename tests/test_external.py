"""Tests for external.py — Open-Meteo weather fetching."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from logger.external import ExternalFetcher, WeatherReading

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LAT = 41.79
_LON = -71.87
_DT = datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)

_OPEN_METEO_RESPONSE: dict[str, Any] = {
    "latitude": 41.8,
    "longitude": -71.875,
    "timezone": "UTC",
    "current_units": {
        "time": "iso8601",
        "wind_speed_10m": "kn",
        "wind_direction_10m": "°",
        "temperature_2m": "°C",
        "surface_pressure": "hPa",
    },
    "current": {
        "time": "2025-08-10T14:00",
        "wind_speed_10m": 12.5,
        "wind_direction_10m": 220.0,
        "temperature_2m": 22.3,
        "surface_pressure": 1013.2,
    },
}


def _make_mock_response(
    payload: dict[str, Any] | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else _OPEN_METEO_RESPONSE
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchWeather:
    async def test_returns_weather_reading_on_success(self) -> None:
        """A valid Open-Meteo response yields a WeatherReading with correct values."""
        mock_resp = _make_mock_response()

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is not None
        assert isinstance(reading, WeatherReading)
        assert reading.wind_speed_kts == pytest.approx(12.5)
        assert reading.wind_direction_deg == pytest.approx(220.0)
        assert reading.air_temp_c == pytest.approx(22.3)
        assert reading.pressure_hpa == pytest.approx(1013.2)
        assert reading.lat == _LAT
        assert reading.lon == _LON

    async def test_timestamp_is_utc(self) -> None:
        """The timestamp on the reading should be UTC-aware."""
        mock_resp = _make_mock_response()

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is not None
        assert reading.timestamp.tzinfo is not None
        assert reading.timestamp == datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)

    async def test_returns_none_on_http_error(self) -> None:
        """Network / HTTP errors return None rather than raising."""
        async with ExternalFetcher() as fetcher:
            with patch.object(
                fetcher._client,  # type: ignore[union-attr]
                "get",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("connection refused"),
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_http_status_error(self) -> None:
        """4xx/5xx responses return None rather than raising."""
        mock_resp = _make_mock_response(status_code=500)

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_missing_key(self) -> None:
        """If the response JSON is missing expected fields, return None."""
        bad_payload: dict[str, Any] = {"current": {"time": "2025-08-10T14:00"}}
        mock_resp = _make_mock_response(payload=bad_payload)

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_empty_response(self) -> None:
        """An empty JSON object returns None."""
        mock_resp = _make_mock_response(payload={})

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_lat_lon_rounded_in_request(self) -> None:
        """Lat/lon are rounded to 4 decimal places in the API request."""
        mock_resp = _make_mock_response()
        captured_params: dict[str, Any] = {}

        async def _fake_get(
            url: str, *, params: dict[str, Any] | None = None, **_: object
        ) -> MagicMock:
            if params:
                captured_params.update(params)
            return mock_resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                await fetcher.fetch_weather(41.789999999, -71.870000001, _DT)

        assert captured_params.get("latitude") == round(41.789999999, 4)
        assert captured_params.get("longitude") == round(-71.870000001, 4)


class TestWeatherStorageRoundTrip:
    async def test_write_and_query(self, storage: object) -> None:
        """WeatherReadings persist and are retrievable by time range."""
        from logger.storage import Storage

        assert isinstance(storage, Storage)

        reading = WeatherReading(
            timestamp=_DT,
            lat=_LAT,
            lon=_LON,
            wind_speed_kts=12.5,
            wind_direction_deg=220.0,
            air_temp_c=22.3,
            pressure_hpa=1013.2,
        )
        await storage.write_weather(reading)

        rows = await storage.query_weather_range(
            datetime(2025, 8, 10, 13, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 10, 15, 0, 0, tzinfo=UTC),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["wind_speed_kts"] == pytest.approx(12.5)
        assert row["wind_dir_deg"] == pytest.approx(220.0)
        assert row["air_temp_c"] == pytest.approx(22.3)
        assert row["pressure_hpa"] == pytest.approx(1013.2)

    async def test_query_outside_range_returns_empty(self, storage: object) -> None:
        """Query with a range that doesn't cover the reading returns empty."""
        from logger.storage import Storage

        assert isinstance(storage, Storage)

        reading = WeatherReading(
            timestamp=_DT,
            lat=_LAT,
            lon=_LON,
            wind_speed_kts=12.5,
            wind_direction_deg=220.0,
            air_temp_c=22.3,
            pressure_hpa=1013.2,
        )
        await storage.write_weather(reading)

        rows = await storage.query_weather_range(
            datetime(2025, 8, 11, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 11, 2, 0, 0, tzinfo=UTC),
        )
        assert rows == []


class TestLatestPosition:
    async def test_returns_none_when_empty(self, storage: object) -> None:
        from logger.storage import Storage

        assert isinstance(storage, Storage)
        result = await storage.latest_position()
        assert result is None

    async def test_returns_most_recent(self, storage: object) -> None:
        from logger.nmea2000 import PGN_POSITION_RAPID, PositionRecord
        from logger.storage import Storage

        assert isinstance(storage, Storage)
        early = datetime(2025, 8, 10, 13, 0, 0, tzinfo=UTC)
        late = datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)
        await storage.write(PositionRecord(PGN_POSITION_RAPID, 5, early, 41.0, -71.0))
        await storage.write(PositionRecord(PGN_POSITION_RAPID, 5, late, 42.0, -72.0))

        result = await storage.latest_position()
        assert result is not None
        assert result["latitude_deg"] == pytest.approx(42.0)
        assert result["longitude_deg"] == pytest.approx(-72.0)
