"""Tests for maneuver_detector.py — tack/gybe detection from 1 Hz instrument data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from helmlog.maneuver_detector import (
    Maneuver,
    ManeuverConfig,
    _hdg_change,
    _twa_bin,
    _tws_bin,
    detect_gybes,
    detect_maneuvers,
    detect_tacks,
)
from helmlog.storage import Storage, StorageConfig

# ---------------------------------------------------------------------------
# Helpers to build synthetic 1 Hz series
# ---------------------------------------------------------------------------

_BASE = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


def _ts(offset_s: int) -> str:
    return (_BASE + timedelta(seconds=offset_s)).isoformat()


def _hdg_series(values: list[float], start_s: int = 0) -> list[dict[str, Any]]:
    """Build synthetic heading rows."""
    return [
        {"ts": _ts(start_s + i), "heading_deg": v, "source_addr": 0} for i, v in enumerate(values)
    ]


def _bsp_series(values: list[float], start_s: int = 0) -> list[dict[str, Any]]:
    """Build synthetic BSP rows."""
    return [
        {"ts": _ts(start_s + i), "speed_kts": v, "source_addr": 0} for i, v in enumerate(values)
    ]


def _twa_series(values: list[float], start_s: int = 0) -> list[dict[str, Any]]:
    """Build synthetic TWA rows (reference=0, boat-referenced)."""
    return [
        {
            "ts": _ts(start_s + i),
            "wind_angle_deg": v,
            "wind_speed_kts": 12.0,
            "reference": 0,
            "source_addr": 0,
        }
        for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestHdgChange:
    def test_straight_ahead(self) -> None:
        assert _hdg_change(45.0, 45.0) == pytest.approx(0.0)

    def test_turn_right(self) -> None:
        assert _hdg_change(10.0, 80.0) == pytest.approx(70.0)

    def test_turn_left(self) -> None:
        assert _hdg_change(80.0, 10.0) == pytest.approx(-70.0)

    def test_wrap_around_north_positive(self) -> None:
        """350° → 010° is a 20° right turn, not a 340° left turn."""
        assert _hdg_change(350.0, 10.0) == pytest.approx(20.0)

    def test_wrap_around_north_negative(self) -> None:
        """010° → 350° is a 20° left turn."""
        assert _hdg_change(10.0, 350.0) == pytest.approx(-20.0)

    def test_exactly_180(self) -> None:
        result = _hdg_change(0.0, 180.0)
        assert abs(result) == pytest.approx(180.0)


class TestBinHelpers:
    def test_tws_bin_floor(self) -> None:
        assert _tws_bin(12.9) == 12

    def test_tws_bin_zero(self) -> None:
        assert _tws_bin(0.0) == 0

    def test_twa_bin_port_starboard_symmetry(self) -> None:
        assert _twa_bin(45.0) == _twa_bin(-45.0)

    def test_twa_bin_five_degree_steps(self) -> None:
        assert _twa_bin(47.0) == 45

    def test_twa_bin_reflex_folded(self) -> None:
        """TWA 200° folds to 160° (360-200)."""
        assert _twa_bin(200.0) == 160


# ---------------------------------------------------------------------------
# Pure detector tests (no storage)
# ---------------------------------------------------------------------------


class TestDetectTacks:
    """Synthetic tests for detect_tacks() with known HDG/BSP/TWA series."""

    def _tack_series(
        self,
        pre_hdg: float = 40.0,
        post_hdg: float = 320.0,
        pre_bsp: float = 6.0,
        min_bsp: float = 2.0,
        twa: float = 42.0,
        pre_s: int = 30,
        tack_s: int = 10,
        post_s: int = 30,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Build a synthetic tack sequence: steady → tack → recovery."""
        hdg_vals = [pre_hdg] * pre_s
        bsp_vals = [pre_bsp] * pre_s
        # During tack: heading gradually changes via shortest arc, BSP dips
        delta = ((post_hdg - pre_hdg) + 360) % 360
        if delta > 180:
            delta -= 360  # take short path
        tack_hdgs = [(pre_hdg + delta * i / tack_s) % 360 for i in range(tack_s)]
        tack_bsps = [
            pre_bsp - (pre_bsp - min_bsp) * (1 - abs(i - tack_s / 2) / (tack_s / 2))
            for i in range(tack_s)
        ]
        hdg_vals += tack_hdgs
        bsp_vals += tack_bsps
        hdg_vals += [post_hdg] * post_s
        bsp_vals += [pre_bsp * 0.95] * post_s  # slight permanent loss is ok
        twa_vals = [twa] * len(hdg_vals)

        hdg = _hdg_series(hdg_vals)
        bsp = _bsp_series(bsp_vals)
        twa = _twa_series(twa_vals)
        return hdg, bsp, twa

    @pytest.mark.asyncio
    async def test_detects_single_tack(self) -> None:
        hdg, bsp, twa = self._tack_series()
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "tack"

    @pytest.mark.asyncio
    async def test_tack_has_required_fields(self) -> None:
        hdg, bsp, twa = self._tack_series()
        maneuvers = await detect_tacks(hdg, bsp, twa)
        m = maneuvers[0]
        assert m.ts is not None
        assert m.loss_kts is not None
        assert m.loss_kts >= 0
        assert m.twa_bin is not None
        assert m.tws_bin is not None

    @pytest.mark.asyncio
    async def test_steady_state_no_tacks(self) -> None:
        """No heading change → no tack detected."""
        hdg = _hdg_series([45.0] * 120)
        bsp = _bsp_series([6.0] * 120)
        twa = _twa_series([42.0] * 120)
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert maneuvers == []

    @pytest.mark.asyncio
    async def test_noisy_hdg_no_false_positives(self) -> None:
        """±2° random-ish noise around 45° shouldn't trigger a tack."""
        import math

        noise = [45.0 + 2.0 * math.sin(i * 0.7) for i in range(120)]
        hdg = _hdg_series(noise)
        bsp = _bsp_series([6.0] * 120)
        twa = _twa_series([42.0] * 120)
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert maneuvers == []

    @pytest.mark.asyncio
    async def test_hdg_wraparound_detected(self) -> None:
        """Tack crossing 0°/360° boundary (e.g. 50° → 330°, -80° turn) is detected."""
        # delta = (330-50+360)%360 = 280 → -80° (crosses 0°: 50→42→...→2→354→...→330)
        hdg, bsp, twa = self._tack_series(pre_hdg=50.0, post_hdg=330.0)
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "tack"

    @pytest.mark.asyncio
    async def test_downwind_hdg_change_not_tack(self) -> None:
        """Large HDG change downwind (TWA=150°) should NOT be a tack."""
        hdg, bsp, twa_rows = self._tack_series(twa=150.0)
        maneuvers = await detect_tacks(hdg, bsp, twa_rows)
        assert maneuvers == []

    @pytest.mark.asyncio
    async def test_multiple_tacks_detected(self) -> None:
        """Two sequential tacks → two Maneuver objects returned."""
        hdg1, bsp1, twa1 = self._tack_series(pre_hdg=40.0, post_hdg=320.0)
        hdg2, bsp2, twa2 = self._tack_series(pre_hdg=320.0, post_hdg=40.0, pre_s=30)
        # Offset the second sequence in time
        offset = len(hdg1)
        hdg2_shifted = [{**r, "ts": _ts(offset + i)} for i, r in enumerate(hdg2)]
        bsp2_shifted = [{**r, "ts": _ts(offset + i)} for i, r in enumerate(bsp2)]
        twa2_shifted = [{**r, "ts": _ts(offset + i)} for i, r in enumerate(twa2)]
        all_hdg = hdg1 + hdg2_shifted
        all_bsp = bsp1 + bsp2_shifted
        all_twa = twa1 + twa2_shifted
        maneuvers = await detect_tacks(all_hdg, all_bsp, all_twa)
        assert len(maneuvers) == 2

    @pytest.mark.asyncio
    async def test_bsp_loss_calculation(self) -> None:
        """BSP loss is non-negative and less than pre-maneuver BSP."""
        hdg, bsp, twa = self._tack_series(pre_bsp=6.5, min_bsp=1.5)
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        loss = maneuvers[0].loss_kts
        assert loss is not None
        assert 0 < loss < 6.5

    @pytest.mark.asyncio
    async def test_tws_twa_bins_match_polar_convention(self) -> None:
        """Bin values use the same convention as polar.py."""
        from helmlog.polar import _twa_bin as polar_twa_bin
        from helmlog.polar import _tws_bin as polar_tws_bin

        hdg, bsp, twa = self._tack_series(twa=42.0)
        maneuvers = await detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        m = maneuvers[0]
        assert m.twa_bin == polar_twa_bin(42.0)
        assert m.tws_bin == polar_tws_bin(12.0)  # default TWS in _twa_series


class TestDetectGybes:
    """Synthetic tests for detect_gybes() — downwind maneuvers."""

    @pytest.mark.asyncio
    async def test_detects_single_gybe(self) -> None:
        """60°+ downwind HDG change → classified as gybe, not tack."""
        pre_hdg = 180.0
        post_hdg = 120.0
        hdg_vals = [pre_hdg] * 30
        bsp_vals = [7.0] * 30
        twa_vals = [160.0] * 30
        for i in range(10):
            hdg_vals.append(pre_hdg + (post_hdg - pre_hdg) * i / 10)
            bsp_vals.append(7.0 - 2.0 * (1 - abs(i - 5) / 5))
            twa_vals.append(160.0)
        hdg_vals += [post_hdg] * 30
        bsp_vals += [6.8] * 30
        twa_vals += [160.0] * 30

        hdg = _hdg_series(hdg_vals)
        bsp = _bsp_series(bsp_vals)
        twa = _twa_series(twa_vals)
        maneuvers = await detect_gybes(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "gybe"

    @pytest.mark.asyncio
    async def test_upwind_change_not_gybe(self) -> None:
        """Upwind tack-like HDG change should NOT be classified as gybe."""
        hdg_vals = [40.0] * 30
        bsp_vals = [6.0] * 30
        twa_vals = [42.0] * 30
        for i in range(10):
            hdg_vals.append(40.0 + (320.0 - 40.0) * i / 10)
            bsp_vals.append(3.0)
            twa_vals.append(42.0)
        hdg_vals += [320.0] * 30
        bsp_vals += [5.8] * 30
        twa_vals += [42.0] * 30
        hdg = _hdg_series(hdg_vals)
        bsp = _bsp_series(bsp_vals)
        twa = _twa_series(twa_vals)
        maneuvers = await detect_gybes(hdg, bsp, twa)
        assert maneuvers == []

    @pytest.mark.asyncio
    async def test_steady_state_no_gybes(self) -> None:
        hdg = _hdg_series([180.0] * 120)
        bsp = _bsp_series([7.0] * 120)
        twa = _twa_series([160.0] * 120)
        maneuvers = await detect_gybes(hdg, bsp, twa)
        assert maneuvers == []


# ---------------------------------------------------------------------------
# Integration tests with Storage
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def seeded_session(storage: Storage) -> int:
    """Create a race session with synthetic tack data in storage."""
    from helmlog.nmea2000 import (
        PGN_SPEED_THROUGH_WATER,
        PGN_VESSEL_HEADING,
        PGN_WIND_DATA,
        HeadingRecord,
        SpeedRecord,
        WindRecord,
    )

    # Create race
    db = storage._conn()
    start = _BASE
    end = _BASE + timedelta(minutes=5)
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("TestRace", "TestEvent", 1, "2024-06-15", start.isoformat(), end.isoformat(), "race"),
    )
    await db.commit()
    session_id = cur.lastrowid
    assert session_id is not None

    # Write synthetic instrument data: tack at t=60s
    total_s = 300
    pre_hdg, post_hdg = 40.0, 320.0
    for i in range(total_s):
        ts = start + timedelta(seconds=i)
        if i < 60:
            hdg_val = pre_hdg
        elif i < 70:
            hdg_val = pre_hdg + (post_hdg - pre_hdg) * (i - 60) / 10
        else:
            hdg_val = post_hdg
        bsp_val = 6.0 if i < 60 or i > 75 else 6.0 - 3.0 * (1 - abs((i - 65) / 5))
        twa_val = 42.0

        await storage.write(
            HeadingRecord(
                pgn=PGN_VESSEL_HEADING,
                source_addr=0,
                timestamp=ts,
                heading_deg=hdg_val,
                deviation_deg=None,
                variation_deg=None,
            )
        )
        await storage.write(
            SpeedRecord(pgn=PGN_SPEED_THROUGH_WATER, source_addr=0, timestamp=ts, speed_kts=bsp_val)
        )
        await storage.write(
            WindRecord(
                pgn=PGN_WIND_DATA,
                source_addr=0,
                timestamp=ts,
                wind_speed_kts=12.0,
                wind_angle_deg=twa_val,
                reference=0,
            )
        )

    await storage._flush()
    return int(session_id)


class TestDetectManeuversIntegration:
    @pytest.mark.asyncio
    async def test_detect_and_store_maneuvers(self, seeded_session: int, storage: Storage) -> None:
        """detect_maneuvers writes to storage and returns maneuvers list."""
        maneuvers = await detect_maneuvers(storage, seeded_session)
        assert len(maneuvers) >= 1
        tacks = [m for m in maneuvers if m.type == "tack"]
        assert len(tacks) == 1

    @pytest.mark.asyncio
    async def test_maneuvers_persisted_to_db(self, seeded_session: int, storage: Storage) -> None:
        """After detect_maneuvers, rows appear in maneuvers table."""
        await detect_maneuvers(storage, seeded_session)
        db = storage._conn()
        cur = await db.execute("SELECT * FROM maneuvers WHERE session_id = ?", (seeded_session,))
        rows = await cur.fetchall()
        assert len(rows) >= 1
        assert rows[0]["type"] == "tack"

    @pytest.mark.asyncio
    async def test_detect_maneuvers_idempotent(self, seeded_session: int, storage: Storage) -> None:
        """Re-running detection replaces previous results (same count)."""
        await detect_maneuvers(storage, seeded_session)
        await detect_maneuvers(storage, seeded_session)
        db = storage._conn()
        cur = await db.execute(
            "SELECT COUNT(*) as n FROM maneuvers WHERE session_id = ?", (seeded_session,)
        )
        row = await cur.fetchone()
        # Should be exactly 1 (not 2 from double run)
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_list_maneuvers(self, seeded_session: int, storage: Storage) -> None:
        """list_maneuvers_for_session returns rows from the DB."""
        await detect_maneuvers(storage, seeded_session)
        rows = await storage.list_maneuvers_for_session(seeded_session)
        assert len(rows) >= 1
        assert rows[0]["type"] == "tack"

    @pytest.mark.asyncio
    async def test_missing_session_returns_empty(self, storage: Storage) -> None:
        """detect_maneuvers on a non-existent session returns empty list."""
        result = await detect_maneuvers(storage, 9999)
        assert result == []


class TestManeuverDataclass:
    def test_frozen(self) -> None:
        m = Maneuver(
            type="tack",
            ts=_BASE,
            end_ts=None,
            duration_sec=None,
            loss_kts=None,
            vmg_loss_kts=None,
            tws_bin=None,
            twa_bin=None,
            details={},
        )
        with pytest.raises((AttributeError, TypeError)):
            m.type = "gybe"  # type: ignore[misc]

    def test_defaults_none(self) -> None:
        m = Maneuver(
            type="gybe",
            ts=_BASE,
            end_ts=None,
            duration_sec=None,
            loss_kts=None,
            vmg_loss_kts=None,
            tws_bin=None,
            twa_bin=None,
            details={},
        )
        assert m.vmg_loss_kts is None


class TestManeuverConfig:
    def test_defaults(self) -> None:
        cfg = ManeuverConfig()
        assert cfg.tack_hdg_threshold_deg == 70.0
        assert cfg.gybe_hdg_threshold_deg == 60.0

    def test_custom_threshold(self) -> None:
        cfg = ManeuverConfig(tack_hdg_threshold_deg=80.0)
        assert cfg.tack_hdg_threshold_deg == 80.0
