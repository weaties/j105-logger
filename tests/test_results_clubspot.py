"""ClubspotProvider tests against saved fixture (#459, R36)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from helmlog.results.base import Regatta, RegattaResults
from helmlog.results.clubspot import ClubspotProvider, _parse_class_payload

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "clubspot"
_REGATTA_ID = "wYFzQvmG4R"
_J105_CLASS_ID = "7q1o9ikhPH"


@pytest.fixture
def j105_payload() -> dict:
    return json.loads((_FIXTURES / "wYFzQvmG4R_J105.json").read_text())


# ---------------------------------------------------------------------------
# Low-level parser
# ---------------------------------------------------------------------------


def test_parse_j105_races(j105_payload: dict) -> None:
    races, standings = _parse_class_payload(j105_payload, _REGATTA_ID)
    assert len(races) == 2
    assert races[0].race_number == 1
    assert races[1].race_number == 2
    assert all(r.class_name == "J/105" for r in races)


def test_parse_j105_finishes_per_race(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        assert len(r.finishes) == 15


def test_parse_j105_standings(j105_payload: dict) -> None:
    _, standings = _parse_class_payload(j105_payload, _REGATTA_ID)
    assert len(standings) == 15
    assert all(s.class_name == "J/105" for s in standings)
    totals = [s.total_points for s in standings if s.total_points is not None]
    assert len(totals) == 15


def test_parse_j105_sail_numbers(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    sails = {f.sail_number for r in races for f in r.finishes}
    assert "482" in sails  # Panic


def test_parse_status_codes(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    all_statuses = {f.status_code for r in races for f in r.finishes if f.status_code}
    assert "RET" in all_statuses


def test_parse_points_assigned(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        for f in r.finishes:
            assert f.points is not None, f"Missing points for {f.sail_number} in {r.name}"


def test_parse_race_date_extracted(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        assert r.date == "2026-04-09", f"Unexpected date {r.date} for {r.name}"


def test_parse_skipper_names(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    skippers = {f.skipper for r in races for f in r.finishes if f.skipper}
    assert len(skippers) > 0


def test_parse_race_source_ids_unique(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    source_ids = [r.source_id for r in races]
    assert len(source_ids) == len(set(source_ids))


def test_parse_empty_payload() -> None:
    races, standings = _parse_class_payload({"scoresByRegistration": [], "races": []}, "dummy")
    assert races == []
    assert standings == []


# ---------------------------------------------------------------------------
# Provider integration (mock HTTP)
# ---------------------------------------------------------------------------


def _mock_transport(fixture_path: Path) -> httpx.MockTransport:
    payload = fixture_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_fetch_j105() -> None:
    transport = _mock_transport(_FIXTURES / "wYFzQvmG4R_J105.json")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class=_J105_CLASS_ID,
        )
        result = await provider.fetch(regatta)

    assert isinstance(result, RegattaResults)
    assert len(result.races) == 2
    assert len(result.standings) == 15
    assert result.regatta is regatta


@pytest.mark.asyncio
async def test_provider_multi_class() -> None:
    """Provider merges multiple class responses."""
    fixtures = {
        "7q1o9ikhPH": _FIXTURES / "wYFzQvmG4R_J105.json",
        "EvS9obW8uC": _FIXTURES / "wYFzQvmG4R_EvS9obW8uC.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        class_id = request.url.params.get("boatClassIDs", "")
        path = fixtures.get(class_id)
        if path:
            return httpx.Response(
                200,
                content=path.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class="7q1o9ikhPH,EvS9obW8uC",
        )
        result = await provider.fetch(regatta)

    class_names = {r.class_name for r in result.races}
    assert "J/105" in class_names
    assert "J/80" in class_names
    assert len(result.standings) == 15 + 9


@pytest.mark.asyncio
async def test_provider_no_class_ids_raises() -> None:
    async with httpx.AsyncClient() as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="Test",
            default_class=None,
        )
        with pytest.raises(ValueError, match="no class IDs configured"):
            await provider.fetch(regatta)
