"""Build the golden-session test fixture from a real recorded session.

Usage:

    uv run python scripts/build_golden_fixture.py \\
        --db /home/weaties/helmlog/data/logger.db \\
        --session 21 \\
        --out tests/fixtures/golden_session

Run on a Pi (or anywhere with the source DB) to extract the raw
instrument data into a portable JSON fixture. The test (#620) loads
that fixture into an in-memory SQLite and replays the detect + enrich
pipeline.

Per-second downsampling: production sensor rates are 20–100 Hz on some
streams, which would push the fixture far over the 5 MB limit. The
detector itself keys on ``str(ts)[:19]`` and keeps the first sample
per second, so first-per-second downsampling at fixture-build time is
equivalent for detection. Enrichment aggregates over multi-second
windows so the difference is negligible there too. The snapshot is
recorded against the downsampled fixture and is what we lock against.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path

_TABLES = {
    "headings": "heading_deg",
    "speeds": "speed_kts",
    "winds": ("wind_speed_kts", "wind_angle_deg", "reference"),
    "cogsog": ("cog_deg", "sog_kts"),
    "positions": ("latitude_deg", "longitude_deg"),
}


def _downsample_first_per_second(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Keep the first row whose ``ts[:19]`` is unique. Matches the
    detector's per-second binning so aggregates are stable."""
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for r in rows:
        key = str(r["ts"])[:19]
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a HelmLog logger.db")
    parser.add_argument("--session", type=int, required=True, help="races.id")
    parser.add_argument("--out", required=True, help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    race_row = conn.execute(
        "SELECT id, name, event, race_num, date, session_type, start_utc, end_utc"
        " FROM races WHERE id = ?",
        (args.session,),
    ).fetchone()
    if race_row is None:
        raise SystemExit(f"session {args.session} not found in {args.db}")
    race = dict(race_row)
    start = str(race["start_utc"])[:19]
    end = str(race["end_utc"])[:19]
    print(f"session {args.session}: {race['name']!r}")
    print(f"  window: {start} → {end}")

    raw_payload: dict[str, object] = {"race": race, "tables": {}}
    tables_payload = raw_payload["tables"]
    assert isinstance(tables_payload, dict)
    for table, cols in _TABLES.items():
        col_list = (cols,) if isinstance(cols, str) else cols
        col_sql = ", ".join(("ts", "source_addr", *col_list))
        sql = f"SELECT {col_sql} FROM {table} WHERE ts BETWEEN ? AND ? ORDER BY ts"
        rows = [dict(r) for r in conn.execute(sql, (start, end + ".999999")).fetchall()]
        downsampled = _downsample_first_per_second(rows)
        tables_payload[table] = downsampled
        print(f"  {table}: {len(rows)} rows → {len(downsampled)} downsampled")

    raw_path = out_dir / "raw_data.json.gz"
    with gzip.open(raw_path, "wt", compresslevel=9) as f:
        json.dump(raw_payload, f, indent=None, separators=(",", ":"), default=str)
    print(f"wrote {raw_path} ({raw_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
