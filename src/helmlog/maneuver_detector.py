"""Maneuver detection from 1 Hz instrument data (#232).

Detects sailing maneuvers (tacks, gybes) from stored SQLite instrument data and
writes them to the ``maneuvers`` table.  No hardware dependencies — all input
comes from decoded data structures.

Algorithm
---------
1. Build aligned time series from headings / speeds / winds tables.
2. Compute a rolling heading rate-of-change over a short window.
3. Accumulate heading change within a wider detection window.
4. When the accumulated change exceeds the threshold, confirm upwind/downwind
   from the mean TWA in a surrounding context window.
5. Measure BSP loss: min BSP during the event vs mean BSP in the pre-window.
6. Measure duration: start of heading inflection to BSP recovery (90% of pre).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_WIND_REF_BOAT = 0  # wind_angle_deg is TWA (boat-referenced, reference=0)
_WIND_REF_NORTH = 4  # wind_angle_deg is TWD (north-referenced, reference=4)


@dataclass(frozen=True)
class ManeuverConfig:
    """Thresholds for maneuver detection.

    Phase 1 uses conservative defaults; per §23 of federation-design.md,
    auto-calibration after ≥20 sessions is deferred to a future phase.
    """

    # Minimum cumulative heading change to consider a candidate maneuver
    tack_hdg_threshold_deg: float = 70.0
    gybe_hdg_threshold_deg: float = 60.0

    # Window sizes (seconds)
    detection_window_s: int = 15  # accumulate HDG change within this window
    context_window_s: int = 30  # TWA + pre-BSP context window (each side)
    recovery_window_s: int = 30  # look this far ahead for BSP recovery

    # BSP recovery threshold
    bsp_recovery_fraction: float = 0.90  # 90% of pre-maneuver BSP

    # Minimum pre-window samples to trust baseline BSP
    min_baseline_samples: int = 5

    # Minimum jitter filter: ignore HDG changes smaller than this per second
    hdg_noise_threshold_deg: float = 3.0


# ---------------------------------------------------------------------------
# Maneuver dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Maneuver:
    """A single detected sailing maneuver."""

    type: str  # "tack" | "gybe"
    ts: datetime  # UTC, maneuver start
    end_ts: datetime | None  # UTC, BSP recovery time
    duration_sec: float | None  # seconds from start to recovery
    loss_kts: float | None  # BSP loss vs pre-maneuver baseline
    vmg_loss_kts: float | None  # VMG loss (reserved; None for now)
    tws_bin: int | None  # floor(TWS) at maneuver time
    twa_bin: int | None  # 5° bin, folded to [0, 180]
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _hdg_change(from_deg: float, to_deg: float) -> float:
    """Signed heading change in degrees, normalised to [-180, 180].

    Positive = right turn, negative = left turn.  Handles wrap-around.
    """
    delta = (to_deg - from_deg + 360) % 360
    if delta > 180:
        delta -= 360
    return delta


def _tws_bin(tws_kts: float) -> int:
    """Integer TWS bin — matches polar.py convention."""
    return max(0, int(math.floor(tws_kts)))


def _twa_bin(twa_deg: float) -> int:
    """5° TWA bin folded to [0, 180] — matches polar.py convention."""
    twa_abs = abs(twa_deg) % 360
    if twa_abs > 180:
        twa_abs = 360 - twa_abs
    return int(math.floor(twa_abs / 5)) * 5


# ---------------------------------------------------------------------------
# Time-series alignment helpers
# ---------------------------------------------------------------------------


def _index_by_ts(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index rows by truncated-second ISO timestamp key."""
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r["ts"])[:19]
        idx.setdefault(key, r)
    return idx


def _aligned_ts_keys(
    hdg_idx: dict[str, dict[str, Any]],
    bsp_idx: dict[str, dict[str, Any]],
    twa_idx: dict[str, dict[str, Any]],
) -> list[str]:
    """Return sorted timestamp keys present in all three indices."""
    common = set(hdg_idx) & set(bsp_idx) & set(twa_idx)
    return sorted(common)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _min_val(values: list[float]) -> float | None:
    if not values:
        return None
    return min(values)


def _twa_from_row(row: dict[str, Any]) -> float | None:
    """Extract absolute TWA from a wind row (reference 0 or 4)."""
    ref = int(row.get("reference", -1))
    angle = float(row["wind_angle_deg"])
    if ref == _WIND_REF_BOAT:
        return abs(angle) % 360
    # For reference=4 (north-referenced TWD), we'd need heading; skip for now
    return None


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------


def _find_maneuver_candidates(
    ts_keys: list[str],
    hdg_idx: dict[str, dict[str, Any]],
    config: ManeuverConfig,
) -> list[int]:
    """Return indices into ts_keys where a maneuver candidate starts.

    Groups consecutive seconds with significant heading change (above the noise
    threshold) into events, then returns the start index of each event whose
    accumulated absolute change exceeds the minimum detection threshold.
    """
    n = len(ts_keys)
    threshold = min(config.tack_hdg_threshold_deg, config.gybe_hdg_threshold_deg)

    # Step 1: compute per-second HDG deltas (filtered for noise)
    deltas: list[float] = []
    for j in range(n - 1):
        h0 = float(hdg_idx[ts_keys[j]]["heading_deg"])
        h1 = float(hdg_idx[ts_keys[j + 1]]["heading_deg"])
        step = _hdg_change(h0, h1)
        deltas.append(step if abs(step) >= config.hdg_noise_threshold_deg else 0.0)

    # Step 2: group into events — consecutive active (non-zero) seconds
    # with at most 2 s gap allowed (to bridge brief noise gaps)
    candidates: list[int] = []
    i = 0
    while i < len(deltas):
        if abs(deltas[i]) < config.hdg_noise_threshold_deg:
            i += 1
            continue
        # Start of an event
        event_start = i
        total = 0.0
        gap = 0
        j = i
        while j < len(deltas):
            if abs(deltas[j]) >= config.hdg_noise_threshold_deg:
                total += abs(deltas[j])
                gap = 0
            else:
                gap += 1
                if gap > 2:
                    break
            j += 1
        if total >= threshold:
            candidates.append(event_start)
        i = j + 1  # advance past this event
    return candidates


def _build_maneuver(
    start_idx: int,
    ts_keys: list[str],
    hdg_idx: dict[str, dict[str, Any]],
    bsp_idx: dict[str, dict[str, Any]],
    twa_idx: dict[str, dict[str, Any]],
    maneuver_type: str,
    config: ManeuverConfig,
) -> Maneuver | None:
    """Build a Maneuver from a candidate start index."""
    n = len(ts_keys)
    ctx = config.context_window_s
    w = config.detection_window_s
    end_idx = min(start_idx + w, n - 1)

    # Pre-window indices
    pre_start = max(0, start_idx - ctx)
    pre_keys = ts_keys[pre_start:start_idx]

    # Post-window indices (for recovery)
    post_end = min(n, end_idx + config.recovery_window_s)
    post_keys = ts_keys[end_idx:post_end]

    # --- BSP baseline ---
    pre_bsp = [float(bsp_idx[k]["speed_kts"]) for k in pre_keys if k in bsp_idx]
    if len(pre_bsp) < config.min_baseline_samples:
        return None
    baseline_bsp = sum(pre_bsp) / len(pre_bsp)

    # --- BSP during maneuver ---
    during_keys = ts_keys[start_idx : end_idx + 1]
    during_bsp = [float(bsp_idx[k]["speed_kts"]) for k in during_keys if k in bsp_idx]
    min_bsp = min(during_bsp) if during_bsp else baseline_bsp
    loss_kts = round(max(0.0, baseline_bsp - min_bsp), 3)

    # --- TWA / TWS context ---
    ctx_start = max(0, start_idx - ctx)
    ctx_end = min(n, end_idx + ctx)
    ctx_keys = ts_keys[ctx_start:ctx_end]

    twa_values: list[float] = []
    tws_values: list[float] = []
    for k in ctx_keys:
        if k not in twa_idx:
            continue
        row = twa_idx[k]
        twa = _twa_from_row(row)
        if twa is not None:
            twa_values.append(twa)
        tws = float(row.get("wind_speed_kts", 0))
        tws_values.append(tws)

    mean_twa = _mean(twa_values)
    mean_tws = _mean(tws_values)

    # --- BSP recovery time ---
    recovery_threshold = baseline_bsp * config.bsp_recovery_fraction
    end_ts_str: str | None = None
    duration_sec: float | None = None
    for k in post_keys:
        if k not in bsp_idx:
            continue
        if float(bsp_idx[k]["speed_kts"]) >= recovery_threshold:
            end_ts_str = k
            break

    start_ts_str = ts_keys[start_idx]
    if end_ts_str is not None:
        try:
            t0 = datetime.fromisoformat(start_ts_str).replace(tzinfo=UTC)
            t1 = datetime.fromisoformat(end_ts_str).replace(tzinfo=UTC)
            duration_sec = round((t1 - t0).total_seconds(), 1)
        except ValueError:
            pass

    import contextlib

    ts_dt = datetime.fromisoformat(start_ts_str).replace(tzinfo=UTC)
    end_ts_dt: datetime | None = None
    if end_ts_str is not None:
        with contextlib.suppress(ValueError):
            end_ts_dt = datetime.fromisoformat(end_ts_str).replace(tzinfo=UTC)

    return Maneuver(
        type=maneuver_type,
        ts=ts_dt,
        end_ts=end_ts_dt,
        duration_sec=duration_sec,
        loss_kts=loss_kts,
        vmg_loss_kts=None,
        tws_bin=_tws_bin(mean_tws) if mean_tws is not None else None,
        twa_bin=_twa_bin(mean_twa) if mean_twa is not None else None,
        details={"pre_bsp": round(baseline_bsp, 3), "min_bsp": round(min_bsp, 3)},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def detect_tacks(
    hdg: list[dict[str, Any]],
    bsp: list[dict[str, Any]],
    twa: list[dict[str, Any]],
    config: ManeuverConfig | None = None,
) -> list[Maneuver]:
    """Detect tacks from aligned 1 Hz heading, BSP, and TWA series.

    A tack is a heading change >= ``tack_hdg_threshold_deg`` with mean TWA < 90°.
    """
    if config is None:
        config = ManeuverConfig()

    hdg_idx = _index_by_ts(hdg)
    bsp_idx = _index_by_ts(bsp)
    twa_idx = _index_by_ts(twa)
    ts_keys = _aligned_ts_keys(hdg_idx, bsp_idx, twa_idx)

    if len(ts_keys) < config.detection_window_s + config.context_window_s:
        return []

    candidates = _find_maneuver_candidates(ts_keys, hdg_idx, config)
    maneuvers: list[Maneuver] = []

    for idx in candidates:
        # Measure total HDG change in detection + recovery window
        w = config.detection_window_s
        end_idx = min(idx + w, len(ts_keys) - 1)
        total_change = 0.0
        for j in range(idx, end_idx):
            h0 = float(hdg_idx[ts_keys[j]]["heading_deg"])
            h1 = float(hdg_idx[ts_keys[j + 1]]["heading_deg"])
            step = _hdg_change(h0, h1)
            if abs(step) >= config.hdg_noise_threshold_deg:
                total_change += abs(step)

        if total_change < config.tack_hdg_threshold_deg:
            continue

        # Confirm upwind via TWA context
        ctx = config.context_window_s
        ctx_start = max(0, idx - ctx)
        ctx_end = min(len(ts_keys), end_idx + ctx)
        ctx_keys = ts_keys[ctx_start:ctx_end]
        twa_values = [
            v for k in ctx_keys if k in twa_idx and (v := _twa_from_row(twa_idx[k])) is not None
        ]
        if not twa_values:
            continue
        mean_twa = sum(twa_values) / len(twa_values)
        if mean_twa >= 90.0:
            continue  # downwind — not a tack

        m = _build_maneuver(idx, ts_keys, hdg_idx, bsp_idx, twa_idx, "tack", config)
        if m is not None:
            maneuvers.append(m)
            logger.debug("Tack detected at {}", ts_keys[idx])

    return maneuvers


async def detect_gybes(
    hdg: list[dict[str, Any]],
    bsp: list[dict[str, Any]],
    twa: list[dict[str, Any]],
    config: ManeuverConfig | None = None,
) -> list[Maneuver]:
    """Detect gybes from aligned 1 Hz heading, BSP, and TWA series.

    A gybe is a heading change >= ``gybe_hdg_threshold_deg`` with mean TWA >= 90°.
    """
    if config is None:
        config = ManeuverConfig()

    hdg_idx = _index_by_ts(hdg)
    bsp_idx = _index_by_ts(bsp)
    twa_idx = _index_by_ts(twa)
    ts_keys = _aligned_ts_keys(hdg_idx, bsp_idx, twa_idx)

    if len(ts_keys) < config.detection_window_s + config.context_window_s:
        return []

    candidates = _find_maneuver_candidates(ts_keys, hdg_idx, config)
    maneuvers: list[Maneuver] = []

    for idx in candidates:
        w = config.detection_window_s
        end_idx = min(idx + w, len(ts_keys) - 1)
        total_change = 0.0
        for j in range(idx, end_idx):
            h0 = float(hdg_idx[ts_keys[j]]["heading_deg"])
            h1 = float(hdg_idx[ts_keys[j + 1]]["heading_deg"])
            step = _hdg_change(h0, h1)
            if abs(step) >= config.hdg_noise_threshold_deg:
                total_change += abs(step)

        if total_change < config.gybe_hdg_threshold_deg:
            continue

        # Confirm downwind via TWA context
        ctx = config.context_window_s
        ctx_start = max(0, idx - ctx)
        ctx_end = min(len(ts_keys), end_idx + ctx)
        ctx_keys = ts_keys[ctx_start:ctx_end]
        twa_values = [
            v for k in ctx_keys if k in twa_idx and (v := _twa_from_row(twa_idx[k])) is not None
        ]
        if not twa_values:
            continue
        mean_twa = sum(twa_values) / len(twa_values)
        if mean_twa < 90.0:
            continue  # upwind — not a gybe

        m = _build_maneuver(idx, ts_keys, hdg_idx, bsp_idx, twa_idx, "gybe", config)
        if m is not None:
            maneuvers.append(m)
            logger.debug("Gybe detected at {}", ts_keys[idx])

    return maneuvers


async def detect_maneuvers(
    storage: Storage,
    session_id: int,
    config: ManeuverConfig | None = None,
) -> list[Maneuver]:
    """Detect all maneuvers in a session and persist them to the maneuvers table.

    Fetches heading, speed, and wind data for the session from SQLite, runs
    tack and gybe detection, then writes results using
    ``storage.replace_maneuvers_for_session`` (idempotent — replaces any
    previous detection run for this session).

    Returns the detected maneuvers list.
    """
    if config is None:
        config = ManeuverConfig()

    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        logger.warning("detect_maneuvers: session {} not found", session_id)
        return []

    try:
        start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
        end_raw = row["end_utc"]
        fallback_end = start + timedelta(hours=4)
        end = datetime.fromisoformat(str(end_raw)).replace(tzinfo=UTC) if end_raw else fallback_end
    except ValueError:
        logger.warning("detect_maneuvers: session {} has bad timestamps", session_id)
        return []

    hdg = await storage.query_range("headings", start, end)
    bsp = await storage.query_range("speeds", start, end)
    winds_raw = await storage.query_range("winds", start, end)
    # Keep only true wind (reference 0 or 4)
    twa = [w for w in winds_raw if int(w.get("reference", -1)) in (_WIND_REF_BOAT, _WIND_REF_NORTH)]

    logger.info(
        "detect_maneuvers: session={} hdg={} bsp={} wind={}",
        session_id,
        len(hdg),
        len(bsp),
        len(twa),
    )

    tacks = await detect_tacks(hdg, bsp, twa, config)
    gybes = await detect_gybes(hdg, bsp, twa, config)
    all_maneuvers = sorted(tacks + gybes, key=lambda m: m.ts)

    rows = [
        {
            "type": m.type,
            "ts": m.ts.isoformat(),
            "end_ts": m.end_ts.isoformat() if m.end_ts else None,
            "duration_sec": m.duration_sec,
            "loss_kts": m.loss_kts,
            "vmg_loss_kts": m.vmg_loss_kts,
            "tws_bin": m.tws_bin,
            "twa_bin": m.twa_bin,
            "details": m.details,
        }
        for m in all_maneuvers
    ]
    await storage.replace_maneuvers_for_session(session_id, rows)
    logger.info(
        "detect_maneuvers: session={} detected {} tacks, {} gybes",
        session_id,
        len(tacks),
        len(gybes),
    )
    return all_maneuvers
