"""Tests for analysis/maneuvers.py — per-maneuver entry/exit metrics and loss calc."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

from helmlog.analysis.maneuvers import (
    enrich_maneuver,
    enrich_session_maneuvers,
    extract_local_track,
    rank_maneuvers,
)

_BASE_TS = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)


def _series(values: list[float], offset_s: int = 0) -> list[tuple[datetime, float]]:
    return [(_BASE_TS + timedelta(seconds=offset_s + i), v) for i, v in enumerate(values)]


def _const(n: int, v: float, offset_s: int = 0) -> list[tuple[datetime, float]]:
    return _series([v] * n, offset_s=offset_s)


def _straight_positions(
    lat0: float, lon0: float, cog_deg: float, sog_kts: float, n: int, offset_s: int = 0
) -> list[tuple[datetime, float, float]]:
    """Generate positions moving at a constant SOG/COG from (lat0, lon0)."""
    out = []
    R = 6371000.0
    ms = sog_kts * 0.514444
    cog_rad = math.radians(cog_deg)
    for i in range(n):
        d = ms * i  # metres travelled
        dn = d * math.cos(cog_rad)
        de = d * math.sin(cog_rad)
        dlat = math.degrees(dn / R)
        dlon = math.degrees(de / (R * math.cos(math.radians(lat0))))
        out.append((_BASE_TS + timedelta(seconds=offset_s + i), lat0 + dlat, lon0 + dlon))
    return out


class TestEnrichManeuver:
    def test_entry_exit_averages_from_steady_windows(self) -> None:
        # 30s pre @ hdg=10, bsp=6.0, twa=40, tws=12
        # 10s turn (ignored for entry/exit)
        # 30s post @ hdg=280, bsp=5.5, twa=-40 → folded 40, tws=12
        turn_hdg = _series([10 + i * 27 for i in range(10)], 30)
        hdg = _const(30, 10.0) + turn_hdg + _const(30, 280.0, 40)
        bsp = _const(30, 6.0) + _const(10, 3.0, 30) + _const(30, 5.5, 40)
        twa = _const(30, 40.0) + _const(10, 0.0, 30) + _const(30, 40.0, 40)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 10.0, 6.0, 70)

        maneuver_ts = _BASE_TS + timedelta(seconds=30)
        exit_ts = _BASE_TS + timedelta(seconds=40)

        m = enrich_maneuver(
            maneuver_ts=maneuver_ts,
            exit_ts=exit_ts,
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )

        assert m.entry_bsp is not None and abs(m.entry_bsp - 6.0) < 0.01
        assert m.exit_bsp is not None and abs(m.exit_bsp - 5.5) < 0.01
        assert m.entry_hdg is not None and abs(m.entry_hdg - 10.0) < 1.0
        assert m.exit_hdg is not None and abs(m.exit_hdg - 280.0) < 1.0
        assert m.entry_twa is not None and abs(m.entry_twa - 40.0) < 0.1
        assert m.exit_twa is not None and abs(m.exit_twa - 40.0) < 0.1
        assert m.entry_tws is not None and abs(m.entry_tws - 12.0) < 0.01

    def test_min_bsp_captured_during_maneuver(self) -> None:
        hdg = _const(30, 10.0) + _const(10, 10.0, 30) + _const(30, 280.0, 40)
        bsp_during = [6.0, 5.0, 4.0, 3.0, 2.0, 1.5, 2.0, 3.0, 4.0, 5.0]
        bsp = _const(30, 6.0) + _series(bsp_during, 30) + _const(30, 5.5, 40)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 10.0, 6.0, 70)

        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.min_bsp is not None and abs(m.min_bsp - 1.5) < 0.01

    def test_turn_angle_approx_90_for_quarter_turn(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.turn_angle_deg is not None and 80.0 <= abs(m.turn_angle_deg) <= 100.0

    def test_turn_rate_is_angle_over_duration(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.duration_sec is not None and m.duration_sec == 10.0
        assert m.turn_rate_deg_s is not None and 7.0 <= m.turn_rate_deg_s <= 11.0

    def test_distance_loss_zero_for_straight_line(self) -> None:
        # No actual turn — boat keeps going straight; entry-vector projection
        # should show ~0 loss against the idealized path.
        hdg = _const(70, 0.0)
        bsp = _const(70, 6.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.distance_loss_m is not None
        assert abs(m.distance_loss_m) < 1.0

    def test_distance_loss_positive_for_real_tack(self) -> None:
        # Pre-window: bearing 0° at 6 kt for 30s
        # During maneuver: 10s of near-zero progress (boat stalls in tack)
        # Exit: bearing 270° at 5 kt for 30s (ends up well off the entry line)
        pre = _straight_positions(37.0, -122.0, 0.0, 6.0, 30)
        last_pre_ts, last_lat, last_lon = pre[-1]
        # During maneuver: stall — positions barely move
        during = [(last_pre_ts + timedelta(seconds=i + 1), last_lat, last_lon) for i in range(10)]
        last_during = during[-1]
        post = _straight_positions(last_during[1], last_during[2], 270.0, 5.0, 30, offset_s=40)
        positions = pre + during + post

        hdg = _const(30, 0.0) + _const(10, 315.0, 30) + _const(30, 270.0, 40)
        bsp = _const(30, 6.0) + _const(10, 1.0, 30) + _const(30, 5.0, 40)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)

        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.distance_loss_m is not None
        # Boat should have lost at least ~10m of forward progress along the 0° axis.
        assert m.distance_loss_m > 10.0

    def test_missing_data_returns_none_fields_no_crash(self) -> None:
        hdg = _const(70, 10.0)
        bsp = _const(70, 6.0)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=[],
            tws=[],
            positions=[],
        )
        assert m.entry_twa is None
        assert m.entry_tws is None
        assert m.distance_loss_m is None

    def test_exit_ts_none_uses_fallback_window(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=None,
            hdg=hdg,
            bsp=bsp,
            twa=[],
            tws=[],
            positions=[],
        )
        # Should still compute entry_bsp from pre-window
        assert m.entry_bsp is not None and abs(m.entry_bsp - 5.0) < 0.01


class TestExtractLocalTrack:
    def test_entry_aligned_track_has_forward_axis_along_entry_bearing(self) -> None:
        # Straight line heading 090° (due east) at 5 kt for 60s.
        positions = _straight_positions(37.0, -122.0, 90.0, 5.0, 60)
        bsp = _const(60, 5.0)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=20),
            exit_ts=_BASE_TS + timedelta(seconds=30),
            entry_bearing_deg=90.0,
            positions=positions,
            bsp=bsp,
        )
        assert len(track) > 0
        # All points should have forward (y) progress and ~0 cross (x).
        for p in track:
            assert abs(p["x"]) < 1.0  # cross-track < 1 m
        ys = [p["y"] for p in track]
        assert max(ys) > 0  # forward progress exists
        assert min(ys) < 0  # pre-window points have negative forward

    def test_origin_at_maneuver_start(self) -> None:
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 60)
        bsp = _const(60, 5.0)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=35),
            entry_bearing_deg=0.0,
            positions=positions,
            bsp=bsp,
        )
        zero = [p for p in track if p["t"] == 0.0]
        assert len(zero) == 1
        assert abs(zero[0]["x"]) < 0.01 and abs(zero[0]["y"]) < 0.01

    def test_empty_positions_returns_empty(self) -> None:
        track = extract_local_track(
            maneuver_ts=_BASE_TS,
            exit_ts=None,
            entry_bearing_deg=0.0,
            positions=[],
            bsp=[],
        )
        assert track == []

    def test_bsp_attached_when_available(self) -> None:
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 60)
        bsp = _const(60, 4.2)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=35),
            entry_bearing_deg=0.0,
            positions=positions,
            bsp=bsp,
        )
        assert any("bsp" in p and abs(p["bsp"] - 4.2) < 0.01 for p in track)


class TestEnrichSessionManeuversWindRefZero:
    """Regression for a falsy-zero bug where ``reference=0`` wind rows were
    skipped by ``int(r.get("reference", -1) or -1)`` because ``0 or -1 == -1``,
    leaving ``entry_tws`` / ``entry_twa`` blank on every maneuver in a session
    whose B&G feed publishes boat-referenced true wind (the common case).
    """

    @pytest.mark.asyncio
    async def test_reference_zero_wind_rows_populate_entry_tws(self, storage: Storage) -> None:
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)

        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "test-session",
                "test-event",
                1,
                start.date().isoformat(),
                "race",
                start.isoformat(),
                end.isoformat(),
            ),
        )

        # Seed 120s of 1Hz instrument data across a 60-second tack.
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 10.0 if i < 60 else 280.0
            bsp = 6.0 if i < 55 or i > 70 else 3.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, bsp),
            )
            # Crucially: reference=0, the falsy value that used to be dropped.
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 12.5, 40.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )

        # Seed one stored maneuver at t=60.
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "tack",
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
                10.0,
                3.0,
                None,
                12,
                40,
                None,
            ),
        )
        await db.commit()

        enriched, _video = await enrich_session_maneuvers(storage, 1)

        assert len(enriched) == 1
        m = enriched[0]
        assert m["entry_tws"] is not None
        assert abs(m["entry_tws"] - 12.5) < 0.1
        assert m["entry_twa"] is not None
        assert abs(m["entry_twa"] - 40.0) < 1.0

    @pytest.mark.asyncio
    async def test_enrich_reclassifies_large_turn_gybe_as_rounding(self, storage: Storage) -> None:
        """A 'gybe' detected with a 176° turn is really a leeward (Mexican)
        rounding — the boat stays downwind on both sides but the leg
        direction has changed. Enrichment should upgrade the type and
        record the original classification in details."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (2, 'rounding-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # 176° heading swing across the maneuver: 5° → 181°.
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 5.0 if i < 60 else 181.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 5.0),
            )
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 10.0, 130.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        # Stored maneuver was classified by the detector as a 'gybe' because
        # pre/post TWA both > 90° (downwind both sides).
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (2, 'gybe', ?, ?, 10.0, 3.0, NULL, 10, 130, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 2)
        assert len(enriched) == 1
        m = enriched[0]
        assert m["type"] == "rounding"
        assert isinstance(m.get("details"), dict)
        assert m["details"].get("original_type") == "gybe"

    @pytest.mark.asyncio
    async def test_enrich_keeps_normal_tack_classification(self, storage: Storage) -> None:
        """A clean ~90° tack must stay a tack — the rounding-reclassification
        threshold of 130° must not catch normal maneuvers."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (3, 'tack-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # 90° heading swing: 45° → 315° (close-hauled to close-hauled).
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 45.0 if i < 60 else 315.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 6.0),
            )
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 12.0, 45.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (3, 'tack', ?, ?, 10.0, 3.0, NULL, 12, 45, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 3)
        assert len(enriched) == 1
        assert enriched[0]["type"] == "tack"


class TestRankManeuvers:
    def _mk(self, distance_loss: float | None, bsp_loss: float | None) -> dict:
        return {
            "type": "tack",
            "ts": _BASE_TS.isoformat(),
            "distance_loss_m": distance_loss,
            "loss_kts": bsp_loss,
        }

    def test_quartile_labels_assigned(self) -> None:
        items = [self._mk(float(i), float(i) / 10) for i in range(8)]
        ranked = rank_maneuvers(items)
        # Lowest-loss quartile → "good", highest → "bad", middle → "avg"
        labels = [m["rank"] for m in ranked]
        assert "good" in labels and "bad" in labels
        # The highest distance_loss entry should be 'bad'
        bad = next(m for m in ranked if m["distance_loss_m"] == 7.0)
        assert bad["rank"] == "bad"
        good = next(m for m in ranked if m["distance_loss_m"] == 0.0)
        assert good["rank"] == "good"

    def test_empty_input_returns_empty(self) -> None:
        assert rank_maneuvers([]) == []

    def test_all_none_loss_falls_back_to_bsp_loss(self) -> None:
        items = [
            {"type": "tack", "distance_loss_m": None, "loss_kts": 0.1},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 0.5},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 1.0},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 2.0},
        ]
        ranked = rank_maneuvers(items)
        worst = max(ranked, key=lambda m: m["loss_kts"] or 0)
        assert worst["rank"] == "bad"
