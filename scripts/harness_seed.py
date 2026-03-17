"""On-Pi data seeder for the test harness.

Creates test sessions with instrument data in the local SQLite database.
Designed to run on a Pi via SSH from the harness orchestrator.

Usage:
    uv run python scripts/harness_seed.py --co-op-id <id> [--sessions 3]

Requires the helmlog service to be stopped (or DB to be writable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta


async def seed(co_op_id: str, num_sessions: int, start_lat: float, start_lon: float) -> None:
    """Seed sessions with instrument data and share them with the co-op."""
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    db = storage._conn()

    now = datetime.now(UTC)
    tag = now.strftime("%H%M%S")
    results: list[dict[str, object]] = []

    for i in range(num_sessions):
        session_start = now - timedelta(hours=num_sessions - i, minutes=30)
        session_end = session_start + timedelta(minutes=45)
        name = f"Harness-{tag}-R{i + 1}"

        race = await storage.start_race(
            event="Harness Test Regatta",
            start_utc=session_start,
            date_str=session_start.strftime("%Y-%m-%d"),
            race_num=i + 1,
            name=name,
            session_type="race",
        )
        await storage.end_race(race.id, session_end)
        await storage.share_session(race.id, co_op_id)

        # Seed instrument data — 50 points per session
        for j in range(50):
            ts = (session_start + timedelta(seconds=j * 54)).isoformat()
            lat = start_lat + j * 0.00005
            lon = start_lon + j * 0.00005
            heading = 180.0 + (j % 20) * 3.0
            speed = 5.0 + (j % 10) * 0.3

            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, 5, lat, lon, race.id),
            )
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
                " VALUES (?, ?, ?, NULL, NULL)",
                (ts, 5, heading),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 5, speed),
            )

        await db.commit()

        results.append(
            {
                "session_id": race.id,
                "name": name,
                "start_utc": session_start.isoformat(),
                "end_utc": session_end.isoformat(),
                "centroid_lat": start_lat + 25 * 0.00005,
                "centroid_lon": start_lon + 25 * 0.00005,
                "points": 50,
            }
        )

    await storage.close()

    # Make DB group-writable so the helmlog service (which runs as helmlog user
    # in the weaties group) can write to it after the seeder creates it.
    import os
    import stat

    db_path = storage._config.db_path
    if os.path.exists(db_path):
        st = os.stat(db_path)
        os.chmod(db_path, st.st_mode | stat.S_IWGRP)

    print(json.dumps({"sessions": results}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test sessions on a Pi")
    parser.add_argument("--co-op-id", required=True, help="Co-op ID to share sessions with")
    parser.add_argument("--sessions", type=int, default=2, help="Number of sessions to create")
    parser.add_argument("--lat", type=float, default=47.6, help="Starting latitude")
    parser.add_argument("--lon", type=float, default=-122.4, help="Starting longitude")
    args = parser.parse_args()

    asyncio.run(seed(args.co_op_id, args.sessions, args.lat, args.lon))


if __name__ == "__main__":
    main()
