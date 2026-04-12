"""Clubspot results provider (#459).

Parses the JSON API at results.theclubspot.com into normalized
``RegattaResults``.  The API requires one ``boatClassIDs`` param per
request, so each call returns results for a single class.  The provider
merges multiple class responses into one ``RegattaResults``.

The provider takes an ``httpx.AsyncClient`` for testability — tests
inject a mock client that returns saved fixture JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from helmlog.results.base import (
    BoatFinish,
    RaceData,
    Regatta,
    RegattaResults,
    SeriesStanding,
)

if TYPE_CHECKING:
    import httpx

_API_BASE = "https://results.theclubspot.com/clubspot-results-v4"
_TIMEOUT = 30.0


class ClubspotProvider:
    """Fetch race results from Clubspot's JSON API."""

    source_name: str = "clubspot"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ClubspotProvider requires an httpx.AsyncClient")
        return self._client

    async def fetch(self, regatta: Regatta) -> RegattaResults:
        """Fetch all classes for a Clubspot regatta.

        ``regatta.source_id`` is the Clubspot regatta objectId (e.g.
        ``wYFzQvmG4R``).  Class IDs to fetch are stored as a
        comma-separated string in ``regatta.default_class`` (Clubspot
        objectIds, not human names).  If ``default_class`` is empty the
        provider raises ``ValueError``.
        """
        if not regatta.default_class:
            raise ValueError(
                f"Clubspot regatta {regatta.source_id!r} has no class IDs configured "
                f"(set Regatta.default_class to comma-separated Clubspot class objectIds)"
            )

        class_ids = [c.strip() for c in regatta.default_class.split(",") if c.strip()]
        all_races: dict[str, RaceData] = {}
        standings_by_key: dict[tuple[str, str], SeriesStanding] = {}

        for class_id in class_ids:
            payload = await self._fetch_class(regatta.source_id, class_id)
            races, standings = _parse_class_payload(payload, regatta.source_id)
            for r in races:
                key = f"{r.source_id}:{r.class_name}"
                all_races[key] = r
            for s in standings:
                standings_by_key[(s.sail_number, s.class_name)] = s

        return RegattaResults(
            regatta=regatta,
            races=tuple(all_races.values()),
            standings=tuple(standings_by_key.values()),
        )

    async def _fetch_class(self, regatta_id: str, class_id: str) -> dict[str, Any]:
        url = f"{_API_BASE}/{regatta_id}"
        resp = await self._http().get(url, params={"boatClassIDs": class_id}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        logger.debug(
            "Clubspot fetch {}/{}: {} scores",
            regatta_id,
            class_id,
            len(data.get("scoresByRegistration", [])),
        )
        return data


def _parse_class_payload(
    payload: dict[str, Any],
    regatta_id: str,
) -> tuple[list[RaceData], list[SeriesStanding]]:
    """Parse one Clubspot per-class JSON response into normalized types."""
    registrations = payload.get("scoresByRegistration", [])
    if not registrations:
        return [], []

    class_name = (
        registrations[0]
        .get("registrationObject", {})
        .get("boatClassObject", {})
        .get("name", "Unknown")
    )

    race_finishes: dict[int, list[BoatFinish]] = {}
    race_meta: dict[int, dict[str, Any]] = {}
    standings: list[SeriesStanding] = []

    for entry in registrations:
        reg = entry.get("registrationObject", {})
        sail = reg.get("sailNumber", "")
        if not sail:
            continue

        boat_name = reg.get("boatName")
        skipper_parts = [reg.get("firstName", ""), reg.get("lastName", "")]
        skipper = " ".join(p for p in skipper_parts if p).strip() or None
        yacht_club = reg.get("clubName")

        standings.append(
            SeriesStanding(
                sail_number=str(sail),
                class_name=class_name,
                total_points=_to_float(entry.get("total")),
                net_points=_to_float(entry.get("net")),
            )
        )

        for score in entry.get("scoring_data", []):
            race_num = score.get("race_number")
            if race_num is None:
                continue

            start_data = score.get("start_data") or {}
            start_time_str = start_data.get("start_time")
            finish_time_str = score.get("finish_time")

            elapsed_ms = score.get("milliseconds_elapsed")
            elapsed_s = int(elapsed_ms // 1000) if elapsed_ms else None

            corrected_raw = score.get("corrected_time")
            corrected_s: int | None = None
            if isinstance(corrected_raw, (int, float)):
                corrected_s = int(corrected_raw)

            letter = score.get("letterScore")
            status = letter if letter else None

            finish = BoatFinish(
                sail_number=str(sail),
                boat_name=boat_name,
                skipper=skipper,
                yacht_club=yacht_club,
                boat_type=class_name,
                finish_time=finish_time_str,
                start_time=start_time_str,
                elapsed_seconds=elapsed_s,
                corrected_seconds=corrected_s,
                points=_to_float(score.get("points")),
                points_throwout=False,
                status_code=status,
                fleet=class_name,
            )

            race_finishes.setdefault(race_num, []).append(finish)

            if race_num not in race_meta:
                race_date = _extract_date(start_time_str or finish_time_str)
                race_meta[race_num] = {
                    "start_time": start_time_str,
                    "date": race_date,
                    "scores_in_start": score.get("scores"),
                    "start_id": start_data.get("start_id", ""),
                }

    races: list[RaceData] = []
    for race_num in sorted(race_finishes):
        meta = race_meta.get(race_num, {})
        finishes = race_finishes[race_num]
        finishes.sort(key=lambda f: (f.points or 999, f.sail_number))

        source_id = f"{regatta_id}_R{race_num}_{class_name}"

        races.append(
            RaceData(
                source_id=source_id,
                race_number=race_num,
                name=f"Race {race_num}",
                date=meta.get("date", ""),
                class_name=class_name,
                finishes=tuple(finishes),
            )
        )

    return races, standings


def _extract_date(iso_str: str | None) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp, or return empty string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(UTC).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _to_float(val: str | int | float | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
