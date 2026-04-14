"""Tests for derived water current (set/drift) from boat velocity vectors.

Convention: "set" is the compass direction the current is flowing *toward*
(oceanographic), degrees 0..360 with 0=N, 90=E. "drift" is current speed in knots.

Derivation:
    v_ground = (SOG, COG)  # boat velocity over ground
    v_water  = (STW, HDG)  # boat velocity through water
    current  = v_ground - v_water
"""

from __future__ import annotations

import math

import pytest

from helmlog.current import compute_set_drift


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


class TestComputeSetDrift:
    def test_no_current_when_ground_matches_water(self) -> None:
        set_deg, drift_kts = compute_set_drift(sog=5.0, cog=0.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(0.0, abs=1e-9)
        # Set direction is undefined when drift is zero; implementation returns 0.0.
        assert set_deg == 0.0

    def test_pure_eastward_push_on_northbound_boat(self) -> None:
        # Boat steering 5 kt due north through water, but made good 5 kt
        # due east over ground. Water pushed the boat east and south equally:
        # current = (5,0) ground - (0,5) water = (5,-5). Toward SE, mag √50.
        set_deg, drift_kts = compute_set_drift(sog=5.0, cog=90.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(math.sqrt(50), rel=1e-6)
        assert set_deg == pytest.approx(135.0, abs=1e-6)

    def test_head_current_slows_boat(self) -> None:
        # Boat steering north at 5 kt water, only making 3 kt COG due north.
        # Current = (0,3)-(0,5) = (0,-2). Toward south, 2 kt.
        set_deg, drift_kts = compute_set_drift(sog=3.0, cog=0.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(2.0, abs=1e-9)
        assert set_deg == pytest.approx(180.0, abs=1e-6)

    def test_drifting_with_stw_zero(self) -> None:
        # Sails down, STW=0, but making 1 kt south over ground.
        set_deg, drift_kts = compute_set_drift(sog=1.0, cog=180.0, stw=0.0, hdg=0.0)
        assert drift_kts == pytest.approx(1.0, abs=1e-9)
        assert set_deg == pytest.approx(180.0, abs=1e-6)

    def test_wraps_to_360_range(self) -> None:
        # Boat steering east through water, pushed slightly north.
        # water = (5,0) (east component, north component)... using math convention
        # with compass angles, we trust the function to wrap [0, 360).
        set_deg, _ = compute_set_drift(sog=5.05, cog=85.0, stw=5.0, hdg=90.0)
        assert 0.0 <= set_deg < 360.0

    def test_returns_none_for_missing_inputs(self) -> None:
        assert compute_set_drift(sog=None, cog=0.0, stw=5.0, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=None, stw=5.0, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=0.0, stw=None, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=0.0, stw=5.0, hdg=None) is None
