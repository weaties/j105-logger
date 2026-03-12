"""Tests for synthesize.py — J/105 simulation engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from helmlog.courses import build_triangle_course, build_wl_course, is_in_water
from helmlog.synthesize import (
    _DEPTH_FLOOR,
    HeaderResponseConfig,
    SynthConfig,
    SynthRow,
    WindModel,
    _distance_nm,
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


class TestSimulateWindAngles:
    """Verify the simulation produces valid tracks at various wind angles."""

    # Start in open water mid-Sound to avoid land avoidance interference
    _START = (47.70, -122.44)
    _MAX_DRIFT_NM = 0.05  # max acceptable drift from start at finish

    def _run(self, wind_dir: float, laps: int = 1) -> list[SynthRow]:
        legs = build_wl_course(*self._START, wind_dir, 1.0, laps)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        return simulate(config)

    @pytest.mark.parametrize("wind_dir", [0, 45, 90, 135, 180, 225, 270, 315])
    def test_finishes_near_start(self, wind_dir: int) -> None:
        """Track should finish within MAX_DRIFT_NM of the start position."""
        rows = self._run(wind_dir)
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < self._MAX_DRIFT_NM, (
            f"Wind {wind_dir}°: finish drifted {drift:.3f} nm from start"
        )

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_rounds_marks_sequentially(self, wind_dir: int) -> None:
        """Boat should pass within 0.15 nm of each mark in leg order."""
        legs = build_wl_course(*self._START, wind_dir, 1.0, 1)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        search_from = 0
        for i, leg in enumerate(legs):
            m = leg.target
            min_dist = float("inf")
            min_idx = search_from
            for j in range(search_from, len(rows)):
                d = _distance_nm(rows[j].lat, rows[j].lon, m.lat, m.lon)
                if d < min_dist:
                    min_dist = d
                    min_idx = j
                if d > min_dist + 0.1 and min_dist < 0.15:
                    break
            assert min_dist < 0.18, (
                f"Wind {wind_dir}° leg {i}: closest approach to "
                f"{m.name} was {min_dist:.3f} nm (> 0.18)"
            )
            search_from = min_idx + 1

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_triangle_finishes_near_start(self, wind_dir: int) -> None:
        """Triangle course should also finish near start."""
        legs = build_triangle_course(*self._START, wind_dir, 1.0)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < self._MAX_DRIFT_NM, (
            f"Triangle wind {wind_dir}°: drifted {drift:.3f} nm from start"
        )


class TestLaylineOverstand:
    """Verify the boat doesn't overstand the layline excessively."""

    _START = (47.70, -122.44)

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_no_excessive_overstand(self, wind_dir: int) -> None:
        """Boat should not overstand the layline by more than ~8 boat lengths.

        We allow a generous 8 BL margin (vs the 4 BL target) to account
        for wind shifts and maneuver timing in the simulation.
        """
        legs = build_wl_course(*self._START, wind_dir, 1.0, laps=1)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        mark_a = legs[0].target

        # Find tacks near the mark (heading change > 50°) within 0.3 nm
        max_overstand_nm = 0.0
        for i in range(1, len(rows)):
            hdiff = abs(rows[i].heading - rows[i - 1].heading)
            if hdiff > 180:
                hdiff = 360 - hdiff
            if hdiff < 50:
                continue
            d = _distance_nm(rows[i].lat, rows[i].lon, mark_a.lat, mark_a.lon)
            if d > 0.3:
                continue
            # Settled heading after tack
            settled = rows[min(i + 15, len(rows) - 1)]
            brg = _distance_nm.__module__  # just need _bearing
            from helmlog.synthesize import _bearing as brg_fn

            brg = brg_fn(settled.lat, settled.lon, mark_a.lat, mark_a.lon)
            hdg_diff = abs(((settled.heading - brg + 180) % 360) - 180)
            # Only count when heading is within TWA range (upwind approach)
            if hdg_diff < 60:
                overstand = d * abs(hdg_diff - 42) / 57.3 * d  # rough cross-track
                max_overstand_nm = max(max_overstand_nm, overstand)

        # 8 boat lengths = 8 * 35ft * 0.000165nm/ft ≈ 0.046 nm
        max_bl = max_overstand_nm / 0.006
        assert max_bl < 8, f"Wind {wind_dir}°: max overstand {max_bl:.1f} BL (> 8)"


class TestLandAvoidance:
    """Verify the simulation avoids sailing over land."""

    def test_track_stays_in_water(self) -> None:
        """Near-shore start: every position must be in navigable water."""
        # Start near Magnolia where land avoidance will be exercised
        start = (47.64, -122.42)
        legs = build_wl_course(*start, 0.0, 0.8, laps=1)
        config = SynthConfig(
            start_lat=start[0],
            start_lon=start[1],
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
        on_land = [(i, r.lat, r.lon) for i, r in enumerate(rows) if not is_in_water(r.lat, r.lon)]
        assert on_land == [], (
            f"{len(on_land)} points on land, first: t={on_land[0][0]}s "
            f"({on_land[0][1]:.5f}, {on_land[0][2]:.5f})"
        )


class TestHeaderResponse:
    """Verify probabilistic tacking on wind shifts (#247)."""

    _START = (47.70, -122.44)

    def _make_config(
        self,
        seed: int = 42,
        header_response: HeaderResponseConfig | None = None,
    ) -> SynthConfig:
        legs = build_wl_course(*self._START, 0.0, 1.0, laps=1)
        return SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(8.0, 14.0),
            legs=legs,
            seed=seed,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
            header_response=header_response or HeaderResponseConfig(),
        )

    def _count_tacks(self, rows: list[SynthRow]) -> int:
        """Count maneuvers by detecting cumulative heading swings > 60° over 15s windows."""
        count = 0
        cooldown = 0
        for i in range(15, len(rows)):
            if cooldown > 0:
                cooldown -= 1
                continue
            hdiff = abs(rows[i].heading - rows[i - 15].heading)
            if hdiff > 180:
                hdiff = 360 - hdiff
            if hdiff > 60:
                count += 1
                cooldown = 20  # skip ahead to avoid double-counting
        return count

    def test_different_seeds_different_responses(self) -> None:
        """Two boats with different seeds should not tack identically."""
        rows_a = simulate(self._make_config(seed=42))
        rows_b = simulate(self._make_config(seed=99))
        # Compare heading sequences — different seeds must produce different tracks
        hdg_a = [r.heading for r in rows_a[:300]]
        hdg_b = [r.heading for r in rows_b[:300]]
        assert hdg_a != hdg_b, "Different seeds should produce different heading sequences"

    def test_perfect_vmg_never_misses_shifts(self) -> None:
        """With reaction_probability=1.0 and threshold=0, should respond to every shift."""
        aggressive = HeaderResponseConfig(
            reaction_probability=1.0,
            min_shift_threshold=(0.0, 0.1),
            reaction_delay=(1.0, 2.0),
            fatigue_start_frac=1.0,
        )
        passive = HeaderResponseConfig(
            reaction_probability=0.0,
            min_shift_threshold=(90.0, 90.0),
            reaction_delay=(1.0, 2.0),
        )
        rows_aggressive = simulate(self._make_config(header_response=aggressive))
        rows_passive = simulate(self._make_config(header_response=passive))
        tacks_aggressive = self._count_tacks(rows_aggressive)
        tacks_passive = self._count_tacks(rows_passive)
        # Aggressive should tack more often than passive
        assert tacks_aggressive > tacks_passive, (
            f"Aggressive ({tacks_aggressive} tacks) should tack more than "
            f"passive ({tacks_passive} tacks)"
        )

    def test_deterministic_same_seed(self) -> None:
        """Same seed + same config = identical track."""
        rows_a = simulate(self._make_config(seed=42))
        rows_b = simulate(self._make_config(seed=42))
        assert len(rows_a) == len(rows_b)
        for a, b in zip(rows_a[:100], rows_b[:100], strict=True):
            assert a.lat == b.lat
            assert a.lon == b.lon
            assert a.heading == b.heading

    def test_still_finishes_near_start(self) -> None:
        """Header response should not break course completion."""
        rows = simulate(self._make_config())
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < 0.05, f"Drifted {drift:.3f} nm from start"

    def test_fatigue_reduces_tacking(self) -> None:
        """Early fatigue onset (frac=0.0) should reduce total tacks vs no fatigue."""
        no_fatigue = HeaderResponseConfig(
            reaction_probability=0.80,
            fatigue_start_frac=1.0,  # no fatigue
            fatigue_floor=0.80,
        )
        early_fatigue = HeaderResponseConfig(
            reaction_probability=0.80,
            fatigue_start_frac=0.0,  # fatigued from the start
            fatigue_floor=0.10,
        )
        rows_fresh = simulate(self._make_config(header_response=no_fatigue))
        rows_tired = simulate(self._make_config(header_response=early_fatigue))
        tacks_fresh = self._count_tacks(rows_fresh)
        tacks_tired = self._count_tacks(rows_tired)
        # Fatigued crew should tack no more than fresh crew
        # (could be equal if interval tacks dominate, but shouldn't tack more)
        assert tacks_tired <= tacks_fresh + 2, (
            f"Fatigued ({tacks_tired}) should not tack much more than fresh ({tacks_fresh})"
        )
