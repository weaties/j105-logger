"""Tests for race naming logic and storage race/event methods."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.races import (
    RaceConfig,
    build_grafana_url,
    build_race_name,
    default_event_for_date,
    slugify,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Pure function tests (no DB needed)
# ---------------------------------------------------------------------------


def test_race_config_grafana_defaults() -> None:
    """RaceConfig has sensible defaults for Grafana and Signal K fields."""
    cfg = RaceConfig()
    assert cfg.grafana_port == "3001"
    assert cfg.grafana_uid == "helmlog-sailing"
    assert cfg.sk_port == "3000"
    assert cfg.public_url == ""


def test_race_config_grafana_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """RaceConfig reads Grafana fields from environment variables."""
    monkeypatch.setenv("GRAFANA_PORT", "4001")
    monkeypatch.setenv("GRAFANA_DASHBOARD_UID", "custom-uid")
    cfg = RaceConfig()
    assert cfg.grafana_port == "4001"
    assert cfg.grafana_uid == "custom-uid"


def test_race_config_signalk_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """RaceConfig reads SK_PORT from environment variables."""
    monkeypatch.setenv("SK_PORT", "4000")
    cfg = RaceConfig()
    assert cfg.sk_port == "4000"


_RULES = {0: "BallardCup", 2: "CYC"}


def test_default_event_monday() -> None:
    assert default_event_for_date(date(2025, 8, 11), _RULES) == "BallardCup"  # Monday


def test_default_event_wednesday() -> None:
    assert default_event_for_date(date(2025, 8, 13), _RULES) == "CYC"  # Wednesday


def test_default_event_saturday() -> None:
    assert default_event_for_date(date(2025, 8, 9), _RULES) is None  # Saturday


def test_default_event_no_rules() -> None:
    assert default_event_for_date(date(2025, 8, 11)) is None
    assert default_event_for_date(date(2025, 8, 11), {}) is None


def test_build_race_name() -> None:
    assert build_race_name("BallardCup", date(2025, 8, 10), 2) == "20250810-BallardCup-2"


def test_build_race_name_single_digit() -> None:
    assert build_race_name("CYC", date(2025, 8, 13), 1) == "20250813-CYC-1"


def test_build_race_name_practice() -> None:
    name = build_race_name("BallardCup", date(2025, 8, 10), 1, "practice")
    assert name == "20250810-BallardCup-P1"


def test_build_race_name_synthesized() -> None:
    name = build_race_name("BallardCup", date(2025, 8, 10), 2, "synthesized")
    assert name == "20250810-BallardCup-S2"


# ---------------------------------------------------------------------------
# slugify tests (#449)
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("20250810-BallardCup-2") == "20250810-ballardcup-2"


def test_slugify_spaces_and_punctuation() -> None:
    assert slugify("CYC Spring — Race 4") == "cyc-spring-race-4"


def test_slugify_collapses_runs() -> None:
    assert slugify("Hello   ---   World!!!") == "hello-world"


def test_slugify_strips_edges() -> None:
    assert slugify("--foo--bar--") == "foo-bar"


def test_slugify_unicode_stripped() -> None:
    # Non-ASCII characters are replaced with '-' and collapsed.
    assert slugify("Ballard Cüp #1 — ¡finish!") == "ballard-c-p-1-finish"


def test_slugify_empty_input_returns_empty() -> None:
    assert slugify("") == ""
    assert slugify("!!!") == ""


def test_slugify_max_length_truncates_on_dash_boundary() -> None:
    text = "one-two-three-four-five-six-seven-eight-nine-ten-eleven-twelve-thirteen-fourteen"
    out = slugify(text, max_length=30)
    assert len(out) <= 30
    assert not out.endswith("-")
    # Should cut at a dash, not mid-word
    assert out == "one-two-three-four-five-six"


def test_slugify_max_length_no_dash_in_window() -> None:
    # If the first max_length chars contain no dash, hard-truncate.
    out = slugify("abcdefghijklmnopqrstuvwxyz", max_length=10)
    assert out == "abcdefghij"


def test_slugify_already_lowercase_noop() -> None:
    assert slugify("already-a-slug") == "already-a-slug"


# ---------------------------------------------------------------------------
# build_grafana_url tests
# ---------------------------------------------------------------------------

_BASE = "http://corvopi:3001"
_UID = "helmlog-sailing"
_START_MS = 1700000000000
_END_MS = 1700003600000


def test_build_grafana_url_closed_session() -> None:
    """Closed sessions disable auto-refresh (refresh=)."""
    url = build_grafana_url(_BASE, _UID, _START_MS, _END_MS)
    assert url == f"{_BASE}/d/{_UID}/sailing-data?from={_START_MS}&to={_END_MS}&orgId=1&refresh="


def test_build_grafana_url_active_session() -> None:
    """Active sessions keep auto-refresh at 10s (to=now, refresh=10s)."""
    url = build_grafana_url(_BASE, _UID, _START_MS, None)
    assert url == f"{_BASE}/d/{_UID}/sailing-data?from={_START_MS}&to=now&orgId=1&refresh=10s"


def test_build_grafana_url_custom_org_id() -> None:
    """org_id keyword argument is honoured."""
    url = build_grafana_url(_BASE, _UID, _START_MS, _END_MS, org_id=2)
    assert "&orgId=2&" in url


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


# ---------------------------------------------------------------------------
# Slug + rename storage tests (#449)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_assigns_slug(storage: Storage) -> None:
    race = await storage.start_race("CYC Spring", _START1, _DATE, 4, "20250810-CYC Spring-4")
    assert race.slug == "20250810-cyc-spring-4"


@pytest.mark.asyncio
async def test_start_race_slug_collision_appends_suffix(storage: Storage) -> None:
    # Two races with names that slugify to the same thing → second gets -2.
    r1 = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-ballardcup-1")
    r2 = await storage.start_race("BallardCup", _START2, _DATE, 1, "20250810-BallardCup-1")
    assert r1.slug == "20250810-ballardcup-1"
    assert r2.slug == "20250810-ballardcup-1-2"


@pytest.mark.asyncio
async def test_get_race_by_slug_returns_current(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "20250810-CYC-1")
    found = await storage.get_race_by_slug(race.slug)
    assert found is not None
    assert found.id == race.id


@pytest.mark.asyncio
async def test_get_race_by_slug_miss(storage: Storage) -> None:
    assert await storage.get_race_by_slug("nope") is None


@pytest.mark.asyncio
async def test_rename_race_updates_name_and_slug(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 4, "20260408-CYC-4")
    updated, retired = await storage.rename_race(
        race.id, new_name="Ballard Cup #1 — finish line confusion"
    )
    assert updated.name == "Ballard Cup #1 — finish line confusion"
    assert updated.slug == "ballard-cup-1-finish-line-confusion"
    assert retired == "20260408-cyc-4"
    assert updated.renamed_at is not None

    # Retired slug is recorded in history.
    hit = await storage.lookup_retired_slug("20260408-cyc-4")
    assert hit is not None
    assert hit[0] == race.id


@pytest.mark.asyncio
async def test_rename_race_noop_when_same_values(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "20250810-CYC-1")
    updated, retired = await storage.rename_race(race.id, new_name="20250810-CYC-1")
    assert retired is None
    assert updated.renamed_at is None  # no-op leaves renamed_at untouched


@pytest.mark.asyncio
async def test_rename_race_name_collision_raises(storage: Storage) -> None:
    await storage.start_race("CYC", _START1, _DATE, 1, "20250810-CYC-1")
    r2 = await storage.start_race("CYC", _START2, _DATE, 2, "20250810-CYC-2")
    with pytest.raises(ValueError, match="name_taken"):
        await storage.rename_race(r2.id, new_name="20250810-CYC-1")


@pytest.mark.asyncio
async def test_rename_race_blank_name_raises(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "20250810-CYC-1")
    with pytest.raises(ValueError, match="name_blank"):
        await storage.rename_race(race.id, new_name="   ")


@pytest.mark.asyncio
async def test_rename_race_changing_event_regenerates_name(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "20250810-CYC-1")
    updated, _retired = await storage.rename_race(race.id, new_event="BallardCup", new_race_num=3)
    assert updated.name == "20250810-BallardCup-3"
    assert updated.event == "BallardCup"
    assert updated.race_num == 3
    assert updated.slug == "20250810-ballardcup-3"


@pytest.mark.asyncio
async def test_rename_race_slug_collision_with_other_race(storage: Storage) -> None:
    # r1's slug is "cool-race". When r2 is renamed to produce the same slug,
    # r2 should end up with "cool-race-2".
    r1 = await storage.start_race("E", _START1, _DATE, 1, "Cool Race")
    r2 = await storage.start_race("E", _START2, _DATE, 2, "20260408-E-2")
    assert r1.slug == "cool-race"
    updated, _ = await storage.rename_race(r2.id, new_name="Cool  Race!")
    assert updated.name == "Cool  Race!"
    assert updated.slug == "cool-race-2"


@pytest.mark.asyncio
async def test_rename_race_back_to_prior_name_reuses_slug(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "Alpha")
    await storage.rename_race(race.id, new_name="Beta")
    # "alpha" is now in history. Renaming back should reclaim the bare slug
    # and remove the history row (not append -2).
    updated, retired_from_beta = await storage.rename_race(race.id, new_name="Alpha")
    assert updated.slug == "alpha"
    assert retired_from_beta == "beta"
    assert await storage.lookup_retired_slug("alpha") is None


@pytest.mark.asyncio
async def test_lookup_retired_slug_after_rename(storage: Storage) -> None:
    race = await storage.start_race("CYC", _START1, _DATE, 1, "Original")
    await storage.rename_race(race.id, new_name="Renamed")
    hit = await storage.lookup_retired_slug("original")
    assert hit is not None
    rid, ts = hit
    assert rid == race.id
    assert ts.tzinfo is not None


@pytest.mark.asyncio
async def test_purge_expired_slug_history(storage: Storage) -> None:
    from datetime import UTC, datetime, timedelta

    race = await storage.start_race("CYC", _START1, _DATE, 1, "Original")
    await storage.rename_race(race.id, new_name="Renamed")

    # Nothing to purge immediately.
    assert await storage.purge_expired_slug_history(30) == 0

    # Backdate the history row to 40 days ago and purge with 30-day retention.
    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await storage._conn().execute(  # noqa: SLF001
        "UPDATE race_slug_history SET retired_at = ? WHERE slug = ?",
        (old_ts, "original"),
    )
    await storage._conn().commit()  # noqa: SLF001
    assert await storage.purge_expired_slug_history(30) == 1
    assert await storage.lookup_retired_slug("original") is None


@pytest.mark.asyncio
async def test_daily_event_roundtrip(storage: Storage) -> None:
    assert await storage.get_daily_event("2025-08-09") is None

    await storage.set_daily_event("2025-08-09", "Regatta")
    assert await storage.get_daily_event("2025-08-09") == "Regatta"

    # Upsert
    await storage.set_daily_event("2025-08-09", "Regatta2")
    assert await storage.get_daily_event("2025-08-09") == "Regatta2"
