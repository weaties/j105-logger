"""Tests for T2 caching wrapped around fetch_weather + fetch_tide_predictions (#610)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helmlog.cache import WebCache
from helmlog.external import ExternalFetcher

if TYPE_CHECKING:
    from helmlog.storage import Storage


_LAT = 41.79
_LON = -71.87
_DT = datetime(2026, 4, 20, 14, 0, 0, tzinfo=UTC)


_OPEN_METEO_RESPONSE: dict[str, Any] = {
    "current": {
        "time": "2026-04-20T14:00",
        "wind_speed_10m": 12.5,
        "wind_direction_10m": 220.0,
        "temperature_2m": 22.3,
        "surface_pressure": 1013.2,
    },
}

_NOAA_STATIONS: dict[str, Any] = {
    "stations": [
        {"id": "8461490", "name": "New London", "lat": 41.36, "lng": -72.09},
    ]
}

_NOAA_PREDICTIONS: dict[str, Any] = {
    "predictions": [
        {"t": "2026-04-20 00:00", "v": "0.5"},
        {"t": "2026-04-20 01:00", "v": "0.6"},
        {"t": "2026-04-20 02:00", "v": "0.7"},
    ]
}


def _mock_response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# T2 global (race_id=0) primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_global_put_get_round_trip(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put_global("weather", data_hash="h1", value={"v": 1}, ttl_seconds=None)
    assert await cache.t2_get_global("weather", data_hash="h1") == {"v": 1}


@pytest.mark.asyncio
async def test_t2_global_stale_hash_returns_none(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put_global("tides", data_hash="abc", value=[1, 2, 3], ttl_seconds=None)
    assert await cache.t2_get_global("tides", data_hash="def") is None


@pytest.mark.asyncio
async def test_t2_ttl_expires_row(storage: Storage) -> None:
    import asyncio

    cache = WebCache(storage)
    # 1ms TTL — guarantees expiry by the time the next await resolves.
    await cache.t2_put_global("weather", data_hash="h", value={"v": 1}, ttl_seconds=0.001)
    await asyncio.sleep(0.05)

    assert await cache.t2_get_global("weather", data_hash="h") is None

    # Row should be deleted by the lazy-eviction-on-read path.
    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache WHERE key_family = 'weather'")
    assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_t2_null_ttl_persists(storage: Storage) -> None:
    """Passing ttl_seconds=None stores with no expiry — tide predictions use this."""
    cache = WebCache(storage)
    await cache.t2_put_global("tides", data_hash="h", value=[1, 2], ttl_seconds=None)
    assert await cache.t2_get_global("tides", data_hash="h") == [1, 2]


@pytest.mark.asyncio
async def test_t2_race_mutation_does_not_touch_global(storage: Storage) -> None:
    """Global (race_id=0) entries must survive race invalidation hooks."""
    cache = WebCache(storage)
    await cache.t2_put_global("weather", data_hash="h", value={"v": 1}, ttl_seconds=None)

    await cache.invalidate(race_id=42)

    assert await cache.t2_get_global("weather", data_hash="h") == {"v": 1}


# ---------------------------------------------------------------------------
# Weather caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_second_call_hits_cache(storage: Storage) -> None:
    """Two successive weather fetches with the same (lat, lon, hour) → 1 HTTP call."""
    cache = WebCache(storage)
    mock_resp = _mock_response(_OPEN_METEO_RESPONSE)

    async with ExternalFetcher(cache=cache) as fetcher:
        with patch.object(
            fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
        ) as mget:
            r1 = await fetcher.fetch_weather(_LAT, _LON, _DT)
            r2 = await fetcher.fetch_weather(_LAT, _LON, _DT)

    assert r1 is not None
    assert r2 is not None
    assert r1.wind_speed_kts == r2.wind_speed_kts
    assert mget.call_count == 1, "second fetch should have hit the T2 cache"


@pytest.mark.asyncio
async def test_weather_hour_boundary_misses_cache(storage: Storage) -> None:
    """Advancing the hour changes the cache key — second fetch should HTTP again."""
    cache = WebCache(storage)
    mock_resp = _mock_response(_OPEN_METEO_RESPONSE)

    async with ExternalFetcher(cache=cache) as fetcher:
        with patch.object(
            fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
        ) as mget:
            await fetcher.fetch_weather(_LAT, _LON, _DT)
            await fetcher.fetch_weather(_LAT, _LON, _DT.replace(hour=15))

    assert mget.call_count == 2


@pytest.mark.asyncio
async def test_weather_error_is_not_cached(storage: Storage) -> None:
    """A failed fetch must not poison the cache — the next call re-fetches."""
    cache = WebCache(storage)

    async with ExternalFetcher(cache=cache) as fetcher:
        # First call fails
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("boom"),
        ):
            assert await fetcher.fetch_weather(_LAT, _LON, _DT) is None

        # Second call succeeds because the cache has no entry for this key
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response(_OPEN_METEO_RESPONSE),
        ) as mget:
            reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

    assert reading is not None
    assert mget.call_count == 1


# ---------------------------------------------------------------------------
# Tide caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tides_second_call_hits_cache(storage: Storage) -> None:
    """Same (lat, lon, date) tide fetches: second call must skip all HTTPs."""
    cache = WebCache(storage)
    for_date = date(2026, 4, 20)

    async with ExternalFetcher(cache=cache) as fetcher:
        # Prime the station-list cache so both calls skip the /stations.json
        # request and only the /datagetter request is interesting.
        fetcher._stations_cache = _NOAA_STATIONS["stations"]
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response(_NOAA_PREDICTIONS),
        ) as mget:
            r1 = await fetcher.fetch_tide_predictions(_LAT, _LON, for_date)
            r2 = await fetcher.fetch_tide_predictions(_LAT, _LON, for_date)

    assert len(r1) == 3
    assert len(r2) == 3
    assert r1[0].height_m == r2[0].height_m
    assert r1[0].station_id == r2[0].station_id
    assert mget.call_count == 1, "tides cache should have served the second call"


@pytest.mark.asyncio
async def test_tides_different_date_misses(storage: Storage) -> None:
    cache = WebCache(storage)

    async with ExternalFetcher(cache=cache) as fetcher:
        fetcher._stations_cache = _NOAA_STATIONS["stations"]
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response(_NOAA_PREDICTIONS),
        ) as mget:
            await fetcher.fetch_tide_predictions(_LAT, _LON, date(2026, 4, 20))
            await fetcher.fetch_tide_predictions(_LAT, _LON, date(2026, 4, 21))

    assert mget.call_count == 2


@pytest.mark.asyncio
async def test_tides_empty_result_is_not_cached(storage: Storage) -> None:
    """An empty-result fetch (NOAA returned nothing usable) must not poison the cache."""
    cache = WebCache(storage)
    for_date = date(2026, 4, 20)

    async with ExternalFetcher(cache=cache) as fetcher:
        fetcher._stations_cache = _NOAA_STATIONS["stations"]
        # First call: NOAA responds with an error body → empty list
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response({"error": {"message": "no data"}}),
        ):
            assert await fetcher.fetch_tide_predictions(_LAT, _LON, for_date) == []

        # Second call: now the API works — cache shouldn't have poisoned us
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response(_NOAA_PREDICTIONS),
        ) as mget:
            good = await fetcher.fetch_tide_predictions(_LAT, _LON, for_date)

    assert len(good) == 3
    assert mget.call_count == 1


# ---------------------------------------------------------------------------
# Fetcher without cache still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_without_cache_still_works() -> None:
    """ExternalFetcher() (no cache) degrades gracefully — every call hits HTTP."""
    async with ExternalFetcher() as fetcher:
        with patch.object(
            fetcher._client,
            "get",
            new_callable=AsyncMock,
            return_value=_mock_response(_OPEN_METEO_RESPONSE),
        ) as mget:
            await fetcher.fetch_weather(_LAT, _LON, _DT)
            await fetcher.fetch_weather(_LAT, _LON, _DT)

    assert mget.call_count == 2
