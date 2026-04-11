"""Session and Race repository."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from loguru import logger

from .base import BaseRepository

if TYPE_CHECKING:
    from helmlog.races import Race


class SessionRepository(BaseRepository):
    """Manages races, practices, and other logging sessions."""

    _RACE_COLS = (
        "id, name, event, race_num, date, start_utc, end_utc, session_type, slug, renamed_at"
    )

    @staticmethod
    def _row_to_race(row: Any) -> Race:  # noqa: ANN401
        from helmlog.races import Race as _Race

        return _Race(
            id=row["id"],
            name=row["name"],
            event=row["event"],
            race_num=row["race_num"],
            date=row["date"],
            start_utc=datetime.fromisoformat(row["start_utc"]),
            end_utc=datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
            session_type=row["session_type"],
            slug=row["slug"] or "",
            renamed_at=(datetime.fromisoformat(row["renamed_at"]) if row["renamed_at"] else None),
        )

    async def count_sessions_for_date(self, date_str: str, session_type: str) -> int:
        """Return the count of sessions of the given type for a UTC date string."""
        cur = await self._read_conn().execute(
            "SELECT COUNT(*) FROM races WHERE date = ? AND session_type = ?",
            (date_str, session_type),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_event_rules(self) -> list[dict[str, Any]]:
        """Return all day-of-week event rules, ordered by weekday."""
        cur = await self._read_conn().execute(
            "SELECT weekday, event_name FROM event_rules ORDER BY weekday"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_event_rule(self, weekday: int) -> str | None:
        """Return the event name for a weekday (0=Mon … 6=Sun), or None."""
        cur = await self._read_conn().execute(
            "SELECT event_name FROM event_rules WHERE weekday = ?",
            (weekday,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def get_daily_event(self, date_str: str) -> str | None:
        """Look up a stored custom event name for the given UTC date."""
        cur = await self._read_conn().execute(
            "SELECT event_name FROM daily_events WHERE date = ?", (date_str,)
        )
        row = await cur.fetchone()
        return row["event_name"] if row else None

    async def get_current_race(self) -> Race | None:
        """Return the most recent race with no end_utc, or None."""
        cur = await self._read_conn().execute(
            f"SELECT {self._RACE_COLS}"
            " FROM races WHERE end_utc IS NULL ORDER BY start_utc DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return self._row_to_race(row) if row else None

    async def import_race(
        self,
        *,
        name: str,
        event: str,
        race_num: int,
        date_str: str,
        start_utc: datetime,
        end_utc: datetime,
        session_type: str,
        source: str,
        source_id: str,
        peer_fingerprint: str | None = None,
        peer_co_op_id: str | None = None,
    ) -> int:
        """Insert a backfilled race row with provenance metadata. Returns race_id."""
        from helmlog.races import slugify

        db = self._conn()
        now = datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc,"
            "  session_type, source, source_id, imported_at,"
            "  peer_fingerprint, peer_co_op_id, slug)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                name,
                event,
                race_num,
                date_str,
                start_utc.isoformat(),
                end_utc.isoformat(),
                session_type,
                source,
                source_id,
                now,
                peer_fingerprint,
                peer_co_op_id,
            ),
        )
        race_id = cur.lastrowid
        assert race_id is not None
        base = slugify(name) or f"race-{race_id}"
        slug = await self._allocate_slug(base, exclude_race_id=race_id)
        await db.execute("UPDATE races SET slug = ? WHERE id = ?", (slug, race_id))
        await db.commit()
        logger.info("Imported race {} (source={}, source_id={})", race_id, source, source_id)
        return race_id

    async def _allocate_slug(
        self,
        base: str,
        *,
        exclude_race_id: int | None = None,
    ) -> str:
        """Return an unused slug derived from *base*."""
        db = self._conn()
        n = 1
        while True:
            candidate = base if n == 1 else f"{base}-{n}"
            live_cur = await db.execute(
                "SELECT id FROM races WHERE slug = ? AND (? IS NULL OR id != ?)",
                (candidate, exclude_race_id, exclude_race_id),
            )
            if await live_cur.fetchone() is not None:
                n += 1
                continue
            hist_cur = await db.execute(
                "SELECT race_id FROM race_slug_history WHERE slug = ?",
                (candidate,),
            )
            if await hist_cur.fetchone() is not None:
                n += 1
                continue
            return candidate

    async def list_races_for_date(self, date_str: str) -> list[Race]:
        """Return all races for a UTC date string, ordered by start_utc ASC."""
        cur = await self._read_conn().execute(
            f"SELECT {self._RACE_COLS} FROM races WHERE date = ? ORDER BY start_utc ASC",
            (date_str,),
        )
        rows = await cur.fetchall()
        return [self._row_to_race(row) for row in rows]

    async def import_synthesized_data(self, rows: list[Any], *, race_id: int) -> int:
        """Bulk-insert synthesized instrument data from SynthRow objects."""
        db = self._conn()
        src_gps = 3  # synthetic GPS source address
        src_inst = 7  # synthetic instrument source address

        pos_rows = [(r.ts.isoformat(), src_gps, r.lat, r.lon, race_id) for r in rows]
        hdg_rows = [(r.ts.isoformat(), src_inst, r.heading, None, None, race_id) for r in rows]
        spd_rows = [(r.ts.isoformat(), src_inst, r.bsp, race_id) for r in rows]
        cs_rows = [(r.ts.isoformat(), src_gps, r.cog, r.sog, race_id) for r in rows]
        dep_rows = [(r.ts.isoformat(), src_inst, r.depth, None, race_id) for r in rows]
        # True wind (reference=0 = boat-referenced TWA)
        tw_rows = [(r.ts.isoformat(), src_inst, r.tws, r.twa, 0, race_id) for r in rows]
        # Apparent wind (reference=2)
        aw_rows = [(r.ts.isoformat(), src_inst, r.aws, r.awa, 2, race_id) for r in rows]

        await db.executemany(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            pos_rows,
        )
        await db.executemany(
            "INSERT INTO headings"
            " (ts, source_addr, heading_deg, deviation_deg, variation_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            hdg_rows,
        )
        await db.executemany(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, ?, ?, ?)",
            spd_rows,
        )
        await db.executemany(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            cs_rows,
        )
        await db.executemany(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            dep_rows,
        )
        await db.executemany(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            tw_rows,
        )
        await db.executemany(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            aw_rows,
        )
        await db.commit()
        logger.info("Imported {} synthesized data points", len(rows))
        return len(rows)

    async def get_scheduled_start(self) -> dict[str, str] | None:
        """Return the pending scheduled start row, or None."""
        cur = await self._read_conn().execute(
            "SELECT id, scheduled_start_utc, event, session_type, created_at"
            " FROM scheduled_starts LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def save_synth_wind_params(self, session_id: int, params: dict[str, Any]) -> None:
        """Persist wind field constructor parameters for a synthesized session."""
        db = self._conn()
        await db.execute(
            "INSERT OR REPLACE INTO synth_wind_params"
            " (session_id, seed, base_twd, tws_low, tws_high,"
            "  shift_interval_lo, shift_interval_hi,"
            "  shift_magnitude_lo, shift_magnitude_hi,"
            "  ref_lat, ref_lon, duration_s,"
            "  course_type, leg_distance_nm, laps, mark_sequence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                params["seed"],
                params["base_twd"],
                params["tws_low"],
                params["tws_high"],
                params["shift_interval_lo"],
                params["shift_interval_hi"],
                params["shift_magnitude_lo"],
                params["shift_magnitude_hi"],
                params["ref_lat"],
                params["ref_lon"],
                params["duration_s"],
                params["course_type"],
                params["leg_distance_nm"],
                params.get("laps"),
                params.get("mark_sequence"),
            ),
        )
        await db.commit()

    async def save_synth_course_marks(self, session_id: int, marks: list[dict[str, Any]]) -> None:
        """Persist course mark positions for a synthesized session."""
        db = self._conn()
        await db.execute("DELETE FROM synth_course_marks WHERE session_id = ?", (session_id,))
        for m in marks:
            await db.execute(
                "INSERT INTO synth_course_marks"
                " (session_id, mark_key, mark_name, lat, lon)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, m["mark_key"], m["mark_name"], m["lat"], m["lon"]),
            )
        await db.commit()
