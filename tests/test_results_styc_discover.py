"""StycProvider URL auto-discovery tests (#526)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from helmlog.results.styc import (
    _base_url_from,
    _extract_racetitle,
    _source_id_from,
    discover_styc_url,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "results" / "styc" / "ballard" / "race1.htm"
_BASE = "https://race.styc.org/race_info/Ballard_Cup/SeriesI/2026"


def test_base_url_strips_race_page() -> None:
    assert _base_url_from(f"{_BASE}/race1.htm?t=154475") == _BASE


def test_base_url_strips_series_page() -> None:
    assert _base_url_from(f"{_BASE}/series.htm") == _BASE


def test_base_url_leaves_base_dir_alone() -> None:
    assert _base_url_from(f"{_BASE}/") == _BASE
    assert _base_url_from(_BASE) == _BASE


def test_source_id_from_path() -> None:
    assert _source_id_from(_BASE) == "Ballard_Cup_SeriesI_2026"


def test_extract_racetitle_from_fixture() -> None:
    html_text = _FIXTURE.read_text()
    assert _extract_racetitle(html_text) == "2026 Ballard Cup Series I"


@pytest.mark.asyncio
async def test_discover_returns_prefilled_form() -> None:
    payload = _FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = await discover_styc_url(
            f"{_BASE}/race1.htm?t=154475",
            client=client,
        )

    assert discovery.source == "styc"
    assert discovery.source_id == "Ballard_Cup_SeriesI_2026"
    assert discovery.name == "2026 Ballard Cup Series I"
    assert discovery.url == _BASE


@pytest.mark.asyncio
async def test_discover_rejects_non_styc_url() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="STYC"):
            await discover_styc_url("https://example.com/race1.htm", client=client)
