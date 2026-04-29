"""Tests for VenueConfig (#700) — pre-race briefing venue registry."""

from __future__ import annotations

from datetime import time

import pytest

from helmlog.briefings import VenueConfig, get_venue, list_venues


def test_shilshole_seed_is_present() -> None:
    venue = get_venue("shilshole")
    assert venue is not None
    assert venue.venue_id == "shilshole"
    assert venue.venue_name == "Shilshole Bay"
    # Approximately Shilshole Bay (Seattle).
    assert 47.6 < venue.venue_lat < 47.8
    assert -122.5 < venue.venue_lon < -122.3
    assert venue.venue_tz == "America/Los_Angeles"
    assert venue.days_of_week == (0, 2)  # Monday, Wednesday
    assert venue.racing_window_local == (time(18, 0), time(21, 0))
    assert venue.lead_hours == (12, 8, 6, 4, 2, 0)


def test_unknown_venue_returns_none() -> None:
    assert get_venue("nope") is None


def test_list_venues_includes_shilshole() -> None:
    ids = {v.venue_id for v in list_venues()}
    assert "shilshole" in ids


def test_venue_config_is_frozen() -> None:
    venue = get_venue("shilshole")
    assert venue is not None
    # Frozen dataclass — assignment raises FrozenInstanceError (subclass
    # of AttributeError). Match on AttributeError so the assertion isn't
    # tied to a specific subclass.
    with pytest.raises(AttributeError):
        venue.venue_name = "Other"  # type: ignore[misc]


def test_lead_hours_are_sorted_descending() -> None:
    """Lead hours must run from earliest (largest) to latest (smallest).

    The composer relies on this ordering to know which briefing is
    "the latest" for a (venue, date) — the one with the smallest lead.
    """
    venue = get_venue("shilshole")
    assert venue is not None
    assert list(venue.lead_hours) == sorted(venue.lead_hours, reverse=True)


def test_venue_config_constructor_accepts_lists_and_freezes_to_tuples() -> None:
    """Hand-constructed venues (tests / future config) accept loose iterables."""
    v = VenueConfig(
        venue_id="bellingham",
        venue_name="Bellingham Bay",
        venue_lat=48.7519,
        venue_lon=-122.4787,
        venue_tz="America/Los_Angeles",
        days_of_week=[0, 1, 2, 3, 4, 5, 6],
        racing_window_local=(time(13, 0), time(17, 0)),
        lead_hours=[24, 12, 6, 0],
    )
    assert v.days_of_week == (0, 1, 2, 3, 4, 5, 6)
    assert v.lead_hours == (24, 12, 6, 0)
