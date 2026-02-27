"""Tests for race naming logic and storage race/event methods."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from logger.races import RaceConfig, build_race_name, default_event_for_date

if TYPE_CHECKING:
    from logger.storage import Storage


# ---------------------------------------------------------------------------
# Pure function tests (no DB needed)
# ---------------------------------------------------------------------------


def test_race_config_grafana_defaults() -> None:
    """RaceConfig has sensible defaults for Grafana fields."""
    cfg = RaceConfig()
    assert cfg.grafana_url == "http://corvopi:3001"
    assert cfg.grafana_uid == "j105-sailing"


def test_race_config_grafana_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """RaceConfig reads Grafana fields from environment variables."""
    monkeypatch.setenv("GRAFANA_URL", "http://myhost:3001")
    monkeypatch.setenv("GRAFANA_DASHBOARD_UID", "custom-uid")
    cfg = RaceConfig()
    assert cfg.grafana_url == "http://myhost:3001"
    assert cfg.grafana_uid == "custom-uid"


def test_default_event_monday() -> None:
    assert default_event_for_date(date(2025, 8, 11)) == "BallardCup"  # Monday


def test_default_event_wednesday() -> None:
    assert default_event_for_date(date(2025, 8, 13)) == "CYC"  # Wednesday


def test_default_event_saturday() -> None:
    assert default_event_for_date(date(2025, 8, 9)) is None  # Saturday


def test_build_race_name() -> None:
    assert build_race_name("BallardCup", date(2025, 8, 10), 2) == "20250810-BallardCup-2"


def test_build_race_name_single_digit() -> None:
    assert build_race_name("CYC", date(2025, 8, 13), 1) == "20250813-CYC-1"


def test_build_race_name_practice() -> None:
    name = build_race_name("BallardCup", date(2025, 8, 10), 1, "practice")
    assert name == "20250810-BallardCup-P1"


# ---------------------------------------------------------------------------
# Storage race method tests (use in-memory DB via conftest `storage` fixture)
# ---------------------------------------------------------------------------


_DATE = "2025-08-10"
_START1 = datetime(2025, 8, 10, 13, 45, 0, tzinfo=UTC)
_START2 = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)
_END1 = datetime(2025, 8, 10, 14, 5, 0, tzinfo=UTC)
_END2 = datetime(2025, 8, 10, 14, 30, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_start_race_closes_previous(storage: Storage) -> None:
    r1 = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
    assert r1.end_utc is None

    # Starting race 2 should close race 1
    await storage.start_race("BallardCup", _START2, _DATE, 2, "20250810-BallardCup-2")

    races = await storage.list_races_for_date(_DATE)
    closed = next(r for r in races if r.id == r1.id)
    assert closed.end_utc is not None
    assert closed.end_utc == _START2


@pytest.mark.asyncio
async def test_end_race_sets_end_utc(storage: Storage) -> None:
    race = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
    await storage.end_race(race.id, _END1)

    races = await storage.list_races_for_date(_DATE)
    assert races[0].end_utc == _END1


@pytest.mark.asyncio
async def test_get_current_race_returns_open(storage: Storage) -> None:
    await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")

    current = await storage.get_current_race()
    assert current is not None
    assert current.name == "20250810-BallardCup-1"
    assert current.end_utc is None


@pytest.mark.asyncio
async def test_get_current_race_none_when_all_closed(storage: Storage) -> None:
    race = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
    await storage.end_race(race.id, _END1)

    current = await storage.get_current_race()
    assert current is None


@pytest.mark.asyncio
async def test_list_races_for_date_ordered(storage: Storage) -> None:
    await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
    await storage.start_race("BallardCup", _START2, _DATE, 2, "20250810-BallardCup-2")

    races = await storage.list_races_for_date(_DATE)
    assert len(races) == 2
    assert races[0].race_num == 1
    assert races[1].race_num == 2
    assert races[0].start_utc < races[1].start_utc


@pytest.mark.asyncio
async def test_daily_event_roundtrip(storage: Storage) -> None:
    assert await storage.get_daily_event("2025-08-09") is None

    await storage.set_daily_event("2025-08-09", "Regatta")
    assert await storage.get_daily_event("2025-08-09") == "Regatta"

    # Upsert
    await storage.set_daily_event("2025-08-09", "Regatta2")
    assert await storage.get_daily_event("2025-08-09") == "Regatta2"
