"""Golden-session regression test (#620).

One real recorded session's instrument data is committed under
``tests/fixtures/golden_session/`` along with a snapshot of the
expected maneuver list. This test loads the raw fixture into an
in-memory Storage, runs the full detect + enrich pipeline, and
asserts exact equality with the snapshot (within float tolerance).

The synthetic tests in ``tests/test_maneuver_detector.py`` and
``tests/test_analysis_maneuvers.py`` pin individual logic; this test
catches the kind of regression those can't — a threshold tweak that
shifts the detected maneuver count or drifts ``distance_loss_m`` 0.5 m
across the board on real data.

To regenerate the snapshot after an intentional logic change::

    uv run pytest tests/test_golden_session.py --update-golden

The diff in ``expected_maneuvers.json`` should be reviewed and
included in the same PR as the logic change.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

import pytest

from helmlog.analysis.maneuvers import enrich_session_maneuvers
from helmlog.maneuver_detector import detect_maneuvers
from helmlog.storage import Storage, StorageConfig

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden_session"
_RAW_PATH = _FIXTURE_DIR / "raw_data.json.gz"
_SNAPSHOT_PATH = _FIXTURE_DIR / "expected_maneuvers.json"

# Per-field tolerances. Loose enough to survive Mac dev vs Pi CI
# floating-point differences; tight enough to catch any real logic
# change. Tighten if a class of regression slips past in practice.
_TOL_DISTANCE_M = 0.05
_TOL_DURATION_S = 0.1
_TOL_ANGLE_DEG = 0.1

# Snapshot field set. Keep small and stable — adding fields here
# requires regenerating the snapshot. Each field's category drives
# which tolerance it uses below.
_FIELDS_DISTANCE = {"distance_loss_m"}
_FIELDS_DURATION = {"duration_sec", "time_to_head_to_wind_s", "time_to_recover_s"}
_FIELDS_ANGLE = {"turn_angle_deg"}
_FIELDS_TIMESTAMP = {"ts", "end_ts", "head_to_wind_ts"}
_FIELDS_VALUE = {"entry_bsp"}
_FIELDS_LABEL = {"type", "rank"}
_SNAPSHOT_FIELDS = (
    _FIELDS_DISTANCE
    | _FIELDS_DURATION
    | _FIELDS_ANGLE
    | _FIELDS_TIMESTAMP
    | _FIELDS_VALUE
    | _FIELDS_LABEL
)


def _load_raw() -> dict:
    with gzip.open(_RAW_PATH, "rt") as f:
        return json.load(f)


async def _seed_storage(storage: Storage, raw: dict) -> int:
    """Insert the raw fixture rows into ``storage`` and return the
    session id ready to detect against."""
    db = storage._conn()
    race = raw["race"]
    session_id = int(race["id"])
    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            race["name"],
            race["event"],
            race["race_num"],
            race["date"],
            race["session_type"],
            race["start_utc"],
            race["end_utc"],
        ),
    )

    tables = raw["tables"]
    for r in tables.get("headings", []):
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
            (r["ts"], r["source_addr"], r["heading_deg"]),
        )
    for r in tables.get("speeds", []):
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (r["ts"], r["source_addr"], r["speed_kts"]),
        )
    for r in tables.get("winds", []):
        await db.execute(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                r["ts"],
                r["source_addr"],
                r["wind_speed_kts"],
                r["wind_angle_deg"],
                r["reference"],
            ),
        )
    for r in tables.get("cogsog", []):
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (r["ts"], r["source_addr"], r["cog_deg"], r["sog_kts"]),
        )
    for r in tables.get("positions", []):
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (r["ts"], r["source_addr"], r["latitude_deg"], r["longitude_deg"]),
        )
    await db.commit()
    return session_id


def _project(maneuver: dict) -> dict:
    """Return only the snapshot fields, in stable order."""
    return {k: maneuver.get(k) for k in sorted(_SNAPSHOT_FIELDS)}


def _assert_snapshot_match(actual: list[dict], expected: list[dict]) -> None:
    assert len(actual) == len(expected), (
        f"detected {len(actual)} maneuvers, snapshot has {len(expected)}; "
        "rerun with --update-golden if this change is intentional"
    )
    for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
        for field in sorted(_SNAPSHOT_FIELDS):
            av = a.get(field)
            ev = e.get(field)
            if av is None and ev is None:
                continue
            if av is None or ev is None:
                pytest.fail(f"maneuver {i} field {field}: actual={av!r} expected={ev!r}")
            if field in _FIELDS_DISTANCE:
                assert abs(float(av) - float(ev)) <= _TOL_DISTANCE_M, (
                    f"maneuver {i} {field}: |{av} - {ev}| > {_TOL_DISTANCE_M}"
                )
            elif field in _FIELDS_DURATION:
                assert abs(float(av) - float(ev)) <= _TOL_DURATION_S, (
                    f"maneuver {i} {field}: |{av} - {ev}| > {_TOL_DURATION_S}"
                )
            elif field in _FIELDS_ANGLE:
                assert abs(float(av) - float(ev)) <= _TOL_ANGLE_DEG, (
                    f"maneuver {i} {field}: |{av} - {ev}| > {_TOL_ANGLE_DEG}"
                )
            elif field in _FIELDS_TIMESTAMP:
                # Compare ISO timestamps as datetimes within 1 s.
                ad = datetime.fromisoformat(str(av))
                ed = datetime.fromisoformat(str(ev))
                assert abs((ad - ed).total_seconds()) <= 1.0, (
                    f"maneuver {i} {field}: {av} vs {ev} differ by > 1s"
                )
            else:
                assert av == ev, f"maneuver {i} {field}: {av!r} != {ev!r}"


@pytest.mark.asyncio
async def test_golden_session_matches_snapshot(request: pytest.FixtureRequest) -> None:
    raw = _load_raw()

    storage = Storage(StorageConfig(db_path=":memory:"))
    await storage.connect()
    try:
        session_id = await _seed_storage(storage, raw)
        await detect_maneuvers(storage, session_id)
        enriched, _video = await enrich_session_maneuvers(storage, session_id)

        projected = [_project(m) for m in enriched]

        if request.config.getoption("--update-golden"):
            payload = {
                "_note": (
                    "Snapshot of detect+enrich output for the golden session. "
                    "Regenerate with: uv run pytest tests/test_golden_session.py "
                    "--update-golden. See #620."
                ),
                "session": {
                    "id": session_id,
                    "name": raw["race"]["name"],
                    "start_utc": raw["race"]["start_utc"],
                    "end_utc": raw["race"]["end_utc"],
                },
                "maneuvers": projected,
            }
            _SNAPSHOT_PATH.write_text(
                json.dumps(payload, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            print(f"\n  --update-golden: wrote {len(projected)} maneuvers to {_SNAPSHOT_PATH}")
            return

        assert _SNAPSHOT_PATH.exists(), (
            f"snapshot missing at {_SNAPSHOT_PATH}; bootstrap with --update-golden"
        )
        snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        _assert_snapshot_match(projected, snapshot["maneuvers"])

        # Sanity: session 21 has 13 maneuvers per the survey we picked it
        # against. If detection drifts even within tolerance, the count
        # mismatch above will fire first; this is a belt-and-braces check.
        assert len(projected) >= 8, "detection produced suspiciously few maneuvers"
    finally:
        await storage.close()


# Sanity-check the fixture itself so a corrupted gzip or schema drift
# fails fast and clearly rather than buried inside the seeding loop.
def test_fixture_is_loadable() -> None:
    raw = _load_raw()
    assert "race" in raw
    assert "tables" in raw
    for table in ("headings", "speeds", "winds", "cogsog", "positions"):
        assert isinstance(raw["tables"].get(table), list), f"missing table {table}"
