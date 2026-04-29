"""Routes for pre-race briefings (#700).

GET /briefings/{id}            HTML detail page (with OG meta).
GET /briefings/{id}/chart.png  PNG chart written by the briefing job.

The job that creates briefings is in helmlog.briefings.run_briefing_tick
and runs from main.py at scheduler ticks; the routes here are read-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from helmlog.routes._helpers import get_storage, templates, tpl_ctx

if TYPE_CHECKING:
    from helmlog.briefings import Briefing

router = APIRouter()


@router.get("/briefings/{briefing_id}", response_class=HTMLResponse, include_in_schema=False)
async def briefing_detail(request: Request, briefing_id: int) -> Response:
    """Render the briefing detail page (#700)."""
    storage = get_storage(request)
    briefing = await storage.get_briefing_by_id(briefing_id)
    if briefing is None:
        raise HTTPException(status_code=404, detail="briefing not found")

    series = await storage.list_briefings_for_date(
        venue_id=briefing.venue_id, local_date=briefing.local_date
    )
    series_ids = await storage.list_briefing_ids_for_date(
        venue_id=briefing.venue_id, local_date=briefing.local_date
    )

    # Race summary if linked.
    linked_race = None
    if briefing.race_id is not None:
        linked_race = await storage.get_race(briefing.race_id)

    chart_url = f"/briefings/{briefing_id}/chart.png" if briefing.chart_path else None
    headline = _headline(briefing)

    ctx = tpl_ctx(
        request,
        "/briefings",
        briefing=briefing,
        briefing_id=briefing_id,
        series=series,
        series_ids=series_ids,
        linked_race=linked_race,
        chart_url=chart_url,
        headline=headline,
    )
    return templates.TemplateResponse(request, "briefing_detail.html", ctx)


@router.get("/briefings/{briefing_id}/chart.png", include_in_schema=False)
async def briefing_chart(request: Request, briefing_id: int) -> Response:
    """Serve the chart PNG for a briefing, or 404 if it isn't rendered."""
    storage = get_storage(request)
    chart_path = await storage.get_briefing_chart_path(briefing_id)
    if not chart_path:
        raise HTTPException(status_code=404, detail="chart unavailable")
    p = Path(chart_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="chart unavailable")
    return FileResponse(p, media_type="image/png")


def _headline(briefing: Briefing) -> str:
    """One-line summary used as og:description and the page subtitle."""
    forecast = briefing.hourly_forecast
    if not forecast:
        return "Briefing failed — see source error on detail page"
    speeds = [s.wind_speed_kts for s in forecast]
    gusts = [s.wind_gust_kts for s in forecast]
    lo, hi = min(speeds), max(speeds)
    g_hi = max(gusts)
    return f"Wind {lo:.0f}–{hi:.0f} kts (gust {g_hi:.0f}) · pressure {briefing.pressure_trend}"
