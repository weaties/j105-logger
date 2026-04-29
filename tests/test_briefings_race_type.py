"""Tests for forecast session_type extension on Race (#700)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.races import build_race_name

if TYPE_CHECKING:
    from helmlog.storage import Storage


def test_build_race_name_forecast_uses_F_prefix() -> None:
    name = build_race_name("Shilshole", date(2026, 4, 29), 1, session_type="forecast")
    assert name == "20260429-Shilshole-F1"


def test_build_race_name_distinct_prefixes_per_session_type() -> None:
    d = date(2026, 4, 29)
    assert build_race_name("X", d, 1) == "20260429-X-1"
    assert build_race_name("X", d, 1, "practice") == "20260429-X-P1"
    assert build_race_name("X", d, 1, "synthesized") == "20260429-X-S1"
    assert build_race_name("X", d, 1, "forecast") == "20260429-X-F1"


@pytest.mark.asyncio
async def test_storage_round_trips_forecast_session_type(storage: Storage) -> None:
    """A race written with session_type='forecast' reads back unchanged."""
    race = await storage.start_race(
        event="Shilshole",
        start_utc=datetime(2026, 4, 30, 1, 0, tzinfo=UTC),
        date_str="2026-04-30",
        race_num=1,
        name="20260429-Shilshole-F1",
        session_type="forecast",
    )
    assert race.session_type == "forecast"
    fetched = await storage.get_race(race.id)
    assert fetched is not None
    assert fetched.session_type == "forecast"
