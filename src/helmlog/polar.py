"""Polar performance baseline: build and query BSP vs (TWS, TWA) buckets.

Historical (TWS, TWA, BSP) triplets from completed race sessions are bucketed,
then mean and p90 BSP are stored per bin. Live BSP can then be compared against
this baseline to show whether the boat is over or under-performing.
"""

from __future__ import annotations

import contextlib
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TWA_BIN_SIZE = 5

# wind reference values that appear in the winds table
_WIND_REF_BOAT = 0  # wind_angle_deg IS the TWA (true wind, boat-referenced)
_WIND_REF_NORTH = 4  # wind_angle_deg is TWD; need heading to derive TWA

# Replay-grading config (#469). All tunable so we can adjust without a
# schema change. Segment width is read from env so deployments can tweak
# it without code changes.
POLAR_SEGMENT_SECONDS: int = int(os.environ.get("POLAR_SEGMENT_SECONDS", "10"))

# pct_target thresholds for the grade decision table.
GRADE_RED_BELOW: float = 0.90
GRADE_YELLOW_BELOW: float = 0.97
GRADE_GREEN_BELOW: float = 1.05  # [0.97, 1.05) → green
GRADE_SUSPICIOUS_AT: float = 1.20  # ≥ 1.20 → suspicious (likely bad baseline cell)

# Cache invalidation: bumped whenever the polar baseline is rebuilt.
POLAR_BASELINE_VERSION_KEY: str = "polar_baseline_version"

GradeLabel = Literal["green", "yellow", "red", "suspicious", "unknown"]

# ---------------------------------------------------------------------------
# Pure helpers (all unit-testable without Storage)
# ---------------------------------------------------------------------------


def _tws_bin(tws_kts: float) -> int:
    """Return the integer TWS bin (floor of knots, min 0)."""
    return max(0, int(math.floor(tws_kts)))


def _twa_bin(twa_deg: float) -> int:
    """Return the TWA bin: fold to [0, 180) and floor to nearest _TWA_BIN_SIZE."""
    twa_abs = abs(twa_deg) % 360
    if twa_abs > 180:
        twa_abs = 360 - twa_abs
    return int(math.floor(twa_abs / _TWA_BIN_SIZE)) * _TWA_BIN_SIZE


def _compute_twa(
    wind_angle_deg: float,
    reference: int,
    heading_deg: float | None,
) -> float | None:
    """Derive TWA magnitude from a wind record.

    Returns the absolute TWA in [0, 180], or None if the reference is
    unsupported or the required heading is absent.
    """
    if reference == _WIND_REF_BOAT:
        return abs(wind_angle_deg) % 360
    if reference == _WIND_REF_NORTH:
        if heading_deg is None:
            return None
        twa_raw = (wind_angle_deg - heading_deg + 360) % 360
        return twa_raw if twa_raw <= 180 else 360 - twa_raw
    return None  # apparent wind or unknown reference


# ---------------------------------------------------------------------------
# Baseline builder
# ---------------------------------------------------------------------------


async def build_polar_baseline(storage: Storage, min_sessions: int = 3) -> int:
    """Compute polar baseline from all completed race sessions and persist it.

    For each (tws_bin, twa_bin) cell, the baseline is only written when data
    from at least *min_sessions* distinct races contributed.

    Returns:
        Number of bins written to polar_baseline.
    """
    db = storage._conn()

    # 1. Fetch all completed races. vakaros_session_id is pulled so we can
    #    narrow the window to [race_gun, end] for Vakaros-matched races and
    #    exclude pre-start maneuvering from the baseline (#536).
    cur = await db.execute(
        "SELECT id, start_utc, end_utc, vakaros_session_id FROM races WHERE end_utc IS NOT NULL"
    )
    races = list(await cur.fetchall())
    if not races:
        logger.info("Polar: no completed races found; baseline not built")
        await storage.upsert_polar_baseline([], datetime.now(UTC).isoformat())
        current = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
        next_version = (int(current) if current and current.isdigit() else 0) + 1
        await storage.set_setting(POLAR_BASELINE_VERSION_KEY, str(next_version))
        return 0

    # bin_samples[(tws_bin, twa_bin)] = list of (race_id, bsp_kts)
    bin_samples: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)

    for race_row in races:
        race_id = int(race_row["id"])
        try:
            start = datetime.fromisoformat(str(race_row["start_utc"])).replace(tzinfo=UTC)
            end = datetime.fromisoformat(str(race_row["end_utc"])).replace(tzinfo=UTC)
        except ValueError:
            logger.warning("Polar: skipping race {} — bad timestamps", race_id)
            continue

        # Prefer the Vakaros race_start event as the effective gun for this
        # race; fall back to start_utc when no Vakaros match is available.
        # Skips pre-start maneuvering from the baseline (#536).
        vakaros_sid = race_row["vakaros_session_id"]
        if vakaros_sid is not None:
            gun_cur = await db.execute(
                "SELECT vre.ts FROM vakaros_race_events vre"
                " WHERE vre.session_id = ? AND vre.event_type = 'race_start'"
                "   AND vre.ts BETWEEN ? AND ?"
                " ORDER BY vre.ts DESC LIMIT 1",
                (vakaros_sid, race_row["start_utc"], race_row["end_utc"]),
            )
            gun_row = await gun_cur.fetchone()
            if gun_row is not None:
                with contextlib.suppress(ValueError):
                    start = datetime.fromisoformat(str(gun_row["ts"])).replace(tzinfo=UTC)

        speeds = await storage.query_range("speeds", start, end)
        winds = await storage.query_range("winds", start, end)
        headings = await storage.query_range("headings", start, end)

        # Index by truncated second key (first 19 chars of ISO string)
        spd_by_s: dict[str, dict[str, Any]] = {}
        for s in speeds:
            key = str(s["ts"])[:19]
            spd_by_s.setdefault(key, s)

        hdg_by_s: dict[str, dict[str, Any]] = {}
        for h in headings:
            key = str(h["ts"])[:19]
            hdg_by_s.setdefault(key, h)

        # Filter winds: only reference 0 (boat) and 4 (north); skip apparent (2)
        tw_by_s: dict[str, dict[str, Any]] = {}
        for w in winds:
            if int(w.get("reference", -1)) not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
                continue
            key = str(w["ts"])[:19]
            tw_by_s.setdefault(key, w)

        for sk, spd_row in spd_by_s.items():
            wind_row = tw_by_s.get(sk)
            if wind_row is None:
                continue

            ref = int(wind_row.get("reference", -1))
            wind_angle = float(wind_row["wind_angle_deg"])
            tws_kts = float(wind_row["wind_speed_kts"])
            bsp_kts = float(spd_row["speed_kts"])

            hdg_row = hdg_by_s.get(sk)
            heading = float(hdg_row["heading_deg"]) if hdg_row else None

            twa = _compute_twa(wind_angle, ref, heading)
            if twa is None:
                continue

            tb = _tws_bin(tws_kts)
            ab = _twa_bin(twa)
            bin_samples[(tb, ab)].append((race_id, bsp_kts))

    # 2. Compute statistics per bin; enforce min_sessions
    rows_to_write: list[dict[str, Any]] = []
    for (tws_bin, twa_bin), samples in bin_samples.items():
        unique_races = {s[0] for s in samples}
        if len(unique_races) < min_sessions:
            continue
        bsp_values = sorted(s[1] for s in samples)
        n = len(bsp_values)
        mean_bsp = sum(bsp_values) / n
        p90_idx = max(0, math.ceil(0.9 * n) - 1)
        p90_bsp = bsp_values[p90_idx]
        rows_to_write.append(
            {
                "tws_bin": tws_bin,
                "twa_bin": twa_bin,
                "mean_bsp": round(mean_bsp, 4),
                "p90_bsp": round(p90_bsp, 4),
                "session_count": len(unique_races),
                "sample_count": n,
            }
        )

    built_at = datetime.now(UTC).isoformat()
    await storage.upsert_polar_baseline(rows_to_write, built_at)
    # Bump the baseline version so any cached per-segment grades become stale.
    current = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
    next_version = (int(current) if current and current.isdigit() else 0) + 1
    await storage.set_setting(POLAR_BASELINE_VERSION_KEY, str(next_version))
    logger.info(
        "Polar baseline built: {} bins from {} races (baseline_version={})",
        len(rows_to_write),
        len(races),
        next_version,
    )
    return len(rows_to_write)


async def get_polar_baseline_version(storage: Storage) -> int:
    """Return the current polar baseline version (0 if never built)."""
    raw = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
    return int(raw) if raw and raw.isdigit() else 0


# ---------------------------------------------------------------------------
# Live lookup
# ---------------------------------------------------------------------------


async def lookup_polar(
    storage: Storage,
    tws_kts: float,
    twa_deg: float,
    min_sessions: int = 3,
) -> dict[str, Any] | None:
    """Return the polar_baseline row for the given wind condition, or None.

    Returns None if no row exists or the row doesn't meet the *min_sessions*
    threshold (guards against sparse baseline data).
    """
    tb = _tws_bin(tws_kts)
    ab = _twa_bin(twa_deg)
    row = await storage.get_polar_point(tb, ab)
    if row is None:
        return None
    if int(row["session_count"]) < min_sessions:
        return None
    return row


# ---------------------------------------------------------------------------
# Session polar comparison
# ---------------------------------------------------------------------------


@dataclass
class PolarCell:
    """One (TWS, TWA) cell with baseline and session data."""

    tws_bin: int
    twa_bin: int
    baseline_mean_bsp: float | None
    baseline_p90_bsp: float | None
    session_mean_bsp: float | None
    session_sample_count: int
    delta: float | None


@dataclass
class SessionPolarData:
    """Full polar comparison for a session."""

    cells: list[PolarCell]
    tws_bins: list[int]
    twa_bins: list[int]
    session_sample_count: int


async def session_polar_comparison(
    storage: Storage,
    session_id: int,
) -> SessionPolarData | None:
    """Compare a session's BSP performance against the polar baseline.

    Returns None if the session doesn't exist or hasn't ended.
    Returns a SessionPolarData with empty cells if no instrument data is available.
    """
    db = storage._conn()

    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return None

    try:
        start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
        end = datetime.fromisoformat(str(row["end_utc"])).replace(tzinfo=UTC)
    except ValueError:
        return None

    # Load instrument data for this session
    speeds = await storage.query_range("speeds", start, end)
    winds = await storage.query_range("winds", start, end)
    headings = await storage.query_range("headings", start, end)

    spd_by_s: dict[str, dict[str, Any]] = {}
    for s in speeds:
        spd_by_s.setdefault(str(s["ts"])[:19], s)

    hdg_by_s: dict[str, dict[str, Any]] = {}
    for h in headings:
        hdg_by_s.setdefault(str(h["ts"])[:19], h)

    tw_by_s: dict[str, dict[str, Any]] = {}
    for w in winds:
        if int(w.get("reference", -1)) not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
            continue
        tw_by_s.setdefault(str(w["ts"])[:19], w)

    # Bin session samples
    bin_samples: dict[tuple[int, int], list[float]] = defaultdict(list)
    for sk, spd_row in spd_by_s.items():
        wind_row = tw_by_s.get(sk)
        if wind_row is None:
            continue

        ref = int(wind_row.get("reference", -1))
        wind_angle = float(wind_row["wind_angle_deg"])
        tws_kts = float(wind_row["wind_speed_kts"])
        bsp_kts = float(spd_row["speed_kts"])

        hdg_row = hdg_by_s.get(sk)
        heading = float(hdg_row["heading_deg"]) if hdg_row else None

        twa = _compute_twa(wind_angle, ref, heading)
        if twa is None:
            continue

        tb = _tws_bin(tws_kts)
        ab = _twa_bin(twa)
        bin_samples[(tb, ab)].append(bsp_kts)

    # Load full baseline
    baseline: dict[tuple[int, int], dict[str, Any]] = {}
    try:
        bcur = await db.execute(
            "SELECT tws_bin, twa_bin, mean_bsp, p90_bsp"
            " FROM polar_baseline WHERE session_count >= 3"
        )
        for br in await bcur.fetchall():
            baseline[(int(br["tws_bin"]), int(br["twa_bin"]))] = dict(br)
    except Exception:
        pass  # no baseline table on un-migrated DB

    # Merge session + baseline into cells
    all_keys = set(bin_samples.keys()) | set(baseline.keys())
    cells: list[PolarCell] = []
    total_samples = 0

    for key in all_keys:
        tws_b, twa_b = key
        samples = bin_samples.get(key, [])
        bl = baseline.get(key)

        session_mean = round(sum(samples) / len(samples), 4) if samples else None
        bl_mean = float(bl["mean_bsp"]) if bl else None
        bl_p90 = float(bl["p90_bsp"]) if bl else None
        delta = (
            round(session_mean - bl_mean, 4)
            if session_mean is not None and bl_mean is not None
            else None
        )

        n = len(samples)
        total_samples += n

        # Only include cells where the session has data
        if n > 0:
            cells.append(
                PolarCell(
                    tws_bin=tws_b,
                    twa_bin=twa_b,
                    baseline_mean_bsp=bl_mean,
                    baseline_p90_bsp=bl_p90,
                    session_mean_bsp=session_mean,
                    session_sample_count=n,
                    delta=delta,
                )
            )

    cells.sort(key=lambda c: (c.tws_bin, c.twa_bin))
    tws_bins = sorted({c.tws_bin for c in cells})
    twa_bins = sorted({c.twa_bin for c in cells})

    return SessionPolarData(
        cells=cells,
        tws_bins=tws_bins,
        twa_bins=twa_bins,
        session_sample_count=total_samples,
    )


# ---------------------------------------------------------------------------
# Per-segment grading for race replay (#469)
# ---------------------------------------------------------------------------


@dataclass
class GradedSegment:
    """One time-window's worth of polar grading along a session track."""

    segment_index: int
    t_start: datetime
    t_end: datetime
    lat: float | None
    lon: float | None
    tws_kts: float | None
    twa_deg: float | None
    bsp_kts: float | None
    target_bsp_kts: float | None
    pct_target: float | None
    delta_kts: float | None
    grade: GradeLabel

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["t_start"] = self.t_start.isoformat()
        d["t_end"] = self.t_end.isoformat()
        return d


def _grade_from_pct(pct: float | None) -> GradeLabel:
    """Map pct_target → grade per the decision table."""
    if pct is None:
        return "unknown"
    if pct >= GRADE_SUSPICIOUS_AT:
        return "suspicious"
    if pct < GRADE_RED_BELOW:
        return "red"
    if pct < GRADE_YELLOW_BELOW:
        return "yellow"
    return "green"  # [0.97, 1.20)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _interp_position(
    positions: list[dict[str, Any]], t_mid: datetime
) -> tuple[float | None, float | None]:
    """Return interpolated (lat, lon) at *t_mid*, or nearest fix within ±2s."""
    if not positions:
        return None, None
    # Find bracketing fixes
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    for p in positions:
        ts = datetime.fromisoformat(str(p["ts"])).replace(tzinfo=UTC)
        if ts <= t_mid:
            before = {**p, "_dt": ts}
        else:
            after = {**p, "_dt": ts}
            break
    if before is not None and after is not None:
        span = (after["_dt"] - before["_dt"]).total_seconds()
        if span <= 0:
            return float(before["latitude_deg"]), float(before["longitude_deg"])
        f = (t_mid - before["_dt"]).total_seconds() / span
        lat = float(before["latitude_deg"]) + f * (
            float(after["latitude_deg"]) - float(before["latitude_deg"])
        )
        lon = float(before["longitude_deg"]) + f * (
            float(after["longitude_deg"]) - float(before["longitude_deg"])
        )
        return lat, lon
    # Fall back to nearest fix within ±2s
    candidates = [
        (abs((datetime.fromisoformat(str(p["ts"])).replace(tzinfo=UTC) - t_mid).total_seconds()), p)
        for p in positions
    ]
    candidates.sort(key=lambda x: x[0])
    nearest_dt, nearest = candidates[0]
    if nearest_dt <= 2.0:
        return float(nearest["latitude_deg"]), float(nearest["longitude_deg"])
    return None, None


async def grade_session_segments(
    storage: Storage,
    session_id: int,
    polar_source: str = "own",
    segment_seconds: int | None = None,
) -> list[GradedSegment]:
    """Return per-segment polar grading for a completed session.

    Segments are fixed-width windows over the session's [start_utc, end_utc]
    range. Each segment carries averaged conditions, the polar target, and
    a grade label. Results are cached in ``polar_segment_grades`` and
    invalidated when the polar baseline is rebuilt.
    """
    width = segment_seconds or POLAR_SEGMENT_SECONDS
    db = storage._conn()

    # Resolve session bounds
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return []  # req 16
    try:
        start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
        end = datetime.fromisoformat(str(row["end_utc"])).replace(tzinfo=UTC)
    except ValueError:
        return []
    if end <= start:
        return []

    baseline_version = await get_polar_baseline_version(storage)

    # Cache hit?
    cached = await storage.get_polar_segment_grades(session_id, polar_source, baseline_version)
    if cached is not None:
        return [
            GradedSegment(
                segment_index=int(r["segment_index"]),
                t_start=datetime.fromisoformat(str(r["t_start"])).replace(tzinfo=UTC),
                t_end=datetime.fromisoformat(str(r["t_end"])).replace(tzinfo=UTC),
                lat=r["lat"],
                lon=r["lon"],
                tws_kts=r["tws_kts"],
                twa_deg=r["twa_deg"],
                bsp_kts=r["bsp_kts"],
                target_bsp_kts=r["target_bsp_kts"],
                pct_target=r["pct_target"],
                delta_kts=r["delta_kts"],
                grade=r["grade"],
            )
            for r in cached
        ]

    # Load session window data
    speeds = await storage.query_range("speeds", start, end)
    winds = await storage.query_range("winds", start, end)
    headings = await storage.query_range("headings", start, end)
    positions = await storage.query_range("positions", start, end)

    # Detect un-migrated DB: lookup_polar will raise on missing table.
    baseline_missing = False

    def _ts_of(rec: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(rec["ts"])).replace(tzinfo=UTC)

    speeds_dt = [(_ts_of(r), r) for r in speeds]
    winds_dt = [
        (_ts_of(r), r)
        for r in winds
        if int(r.get("reference", -1)) in (_WIND_REF_BOAT, _WIND_REF_NORTH)
    ]
    hdg_dt = [(_ts_of(r), r) for r in headings]

    segments: list[GradedSegment] = []
    grade_hist: dict[str, int] = defaultdict(int)

    n_segments = math.ceil((end - start).total_seconds() / width)
    for idx in range(n_segments):
        seg_start = start + timedelta(seconds=idx * width)
        seg_end = min(end, seg_start + timedelta(seconds=width))
        t_mid = seg_start + (seg_end - seg_start) / 2

        spd_in = [float(r["speed_kts"]) for ts, r in speeds_dt if seg_start <= ts < seg_end]
        wind_in = [(ts, r) for ts, r in winds_dt if seg_start <= ts < seg_end]
        hdg_in = [float(r["heading_deg"]) for ts, r in hdg_dt if seg_start <= ts < seg_end]
        pos_in = [r for ts, r in [(_ts_of(r), r) for r in positions] if seg_start <= ts < seg_end]

        lat, lon = _interp_position(positions, t_mid) if positions else (None, None)

        bsp = _mean(spd_in)

        tws = _mean([float(r["wind_speed_kts"]) for _, r in wind_in])
        # Compute segment TWA from the mean wind angle / mean heading.
        twa: float | None = None
        if wind_in:
            wind_angle_mean = sum(float(r["wind_angle_deg"]) for _, r in wind_in) / len(wind_in)
            ref = int(wind_in[0][1].get("reference", -1))
            heading_mean = _mean(hdg_in) if ref == _WIND_REF_NORTH else None
            twa = _compute_twa(wind_angle_mean, ref, heading_mean)

        target: float | None = None
        pct: float | None = None
        delta: float | None = None
        if bsp is not None and tws is not None and twa is not None and not baseline_missing:
            try:
                lp = await lookup_polar(storage, tws, twa)
            except Exception as e:  # un-migrated DB or other table-missing error
                logger.warning(
                    "Polar grading: baseline lookup failed for session {}: {}", session_id, e
                )
                baseline_missing = True
                lp = None
            if lp is not None:
                target = float(lp["mean_bsp"])
                pct = bsp / target if target > 0 else None
                delta = round(bsp - target, 4)

        if bsp is not None and tws is not None and twa is not None:
            grade = _grade_from_pct(pct)
        else:
            grade = "unknown"
        if lat is None or lon is None:
            grade = "unknown"

        seg = GradedSegment(
            segment_index=idx,
            t_start=seg_start,
            t_end=seg_end,
            lat=lat,
            lon=lon,
            tws_kts=round(tws, 4) if tws is not None else None,
            twa_deg=round(twa, 4) if twa is not None else None,
            bsp_kts=round(bsp, 4) if bsp is not None else None,
            target_bsp_kts=round(target, 4) if target is not None else None,
            pct_target=round(pct, 4) if pct is not None else None,
            delta_kts=delta,
            grade=grade,
        )
        segments.append(seg)
        grade_hist[grade] += 1
        # silence unused
        _ = pos_in

    # Persist cache
    await storage.upsert_polar_segment_grades(
        session_id,
        polar_source,
        [s.to_row() for s in segments],
        baseline_version,
    )

    logger.info(
        "Polar grading: session={} segments={} grades={}",
        session_id,
        len(segments),
        dict(grade_hist),
    )
    return segments
