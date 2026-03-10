"""Tests for synthesize.py — J/105 simulation engine."""

from __future__ import annotations

from datetime import UTC, datetime

from helmlog.courses import build_wl_course
from helmlog.synthesize import (
    _DEPTH_FLOOR,
    SynthConfig,
    SynthRow,
    WindModel,
    apparent_wind,
    interpolate_polar,
    simulate,
)


class TestInterpolatePolar:
    def test_exact_entry(self) -> None:
        twa, bsp = interpolate_polar(10.0, upwind=True)
        assert twa == 42.0
        assert bsp == 6.5

    def test_interpolation(self) -> None:
        twa, bsp = interpolate_polar(9.0, upwind=True)
        assert 42.0 < twa < 43.0
        assert 6.0 < bsp < 6.5

    def test_downwind(self) -> None:
        twa, bsp = interpolate_polar(10.0, upwind=False)
        assert twa == 140.0
        assert bsp == 6.5

    def test_clamp_below(self) -> None:
        twa, bsp = interpolate_polar(2.0, upwind=True)
        assert twa == 44.0  # clamped to TWS=6

    def test_clamp_above(self) -> None:
        twa, bsp = interpolate_polar(25.0, upwind=True)
        assert twa == 39.0  # clamped to TWS=16


class TestApparentWind:
    def test_headwind(self) -> None:
        aws, awa = apparent_wind(10.0, 0.0, 6.0)
        # Head-to-wind: AWS = TWS + BSP
        assert aws > 15.0
        assert awa < 5.0  # nearly head-on

    def test_nonzero_angle(self) -> None:
        aws, awa = apparent_wind(10.0, 45.0, 6.0)
        assert aws > 0
        assert 0 < awa < 45.0


class TestWindModel:
    def test_deterministic_with_seed(self) -> None:
        wm1 = WindModel(base_twd=180, seed=42)
        wm2 = WindModel(base_twd=180, seed=42)
        assert wm1.get(0) == wm2.get(0)
        assert wm1.get(300) == wm2.get(300)

    def test_tws_above_floor(self) -> None:
        wm = WindModel(base_twd=0, tws_low=5, tws_high=6, seed=1)
        for t in range(0, 3600, 60):
            _, tws = wm.get(t)
            assert tws >= 4.0


class TestSimulate:
    def test_produces_data(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        assert len(rows) > 100
        assert all(isinstance(r, SynthRow) for r in rows)

    def test_depth_floor(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        for r in rows:
            assert r.depth >= _DEPTH_FLOOR

    def test_timestamps_ascending(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        for i in range(1, len(rows)):
            assert rows[i].ts > rows[i - 1].ts

    def test_deterministic(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows1 = simulate(config)
        rows2 = simulate(config)
        assert len(rows1) == len(rows2)
        assert rows1[0].lat == rows2[0].lat
        assert rows1[-1].lat == rows2[-1].lat
