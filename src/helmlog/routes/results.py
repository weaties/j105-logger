"""Admin routes for race results import (#459)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

if TYPE_CHECKING:
    from helmlog.storage import Storage

router = APIRouter()


@router.get("/admin/race-results", response_class=HTMLResponse, include_in_schema=False)
async def admin_race_results_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    regattas = await _list_regattas(storage)
    return templates.TemplateResponse(
        request,
        "admin/race_results.html",
        tpl_ctx(request, "/admin/race-results", regattas=regattas),
    )


@router.get("/api/results/regattas", response_class=JSONResponse)
async def api_list_regattas(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    regattas = await _list_regattas(storage)
    return JSONResponse(regattas)


@router.post("/api/results/regattas", response_class=JSONResponse)
async def api_add_regatta(
    request: Request,
    source: str = Form(...),
    source_id: str = Form(...),
    name: str = Form(...),
    url: str = Form(""),
    default_class: str = Form(""),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    from helmlog.results.base import get_provider

    if not get_provider(source):
        raise HTTPException(400, f"Unknown source: {source!r}")

    storage = get_storage(request)
    db = storage._conn()
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, url, default_class, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, source_id, name, url or None, default_class or None, now),
    )
    await db.commit()
    await audit(
        request,
        "results_regatta_add",
        detail=json.dumps(
            {
                "regatta_id": cur.lastrowid,
                "source": source,
                "name": name,
            }
        ),
    )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@router.post("/api/results/regattas/discover", response_class=JSONResponse)
async def api_discover_regatta(
    request: Request,
    source: str = Form("clubspot"),
    url: str = Form(...),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Discover a regatta's name and class list from a pasted URL (#520).

    Currently only supports Clubspot — STYC admins still use the manual
    Add Regatta form.
    """
    import httpx

    from helmlog.results.clubspot import ClubspotProvider

    if source != "clubspot":
        raise HTTPException(400, f"Discovery not supported for source {source!r}")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        provider = ClubspotProvider(client=client)
        try:
            info = await provider.discover_regatta(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Upstream error: {exc}") from exc

    return JSONResponse(
        {
            "source": source,
            "source_id": info.source_id,
            "name": info.name,
            "url": info.url,
            "classes": [{"id": c.id, "name": c.name} for c in info.classes],
        }
    )


@router.delete("/api/results/regattas/{regatta_id}", response_class=JSONResponse)
async def api_delete_regatta(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._conn()
    await db.execute("DELETE FROM series_results WHERE regatta_id = ?", (regatta_id,))
    await db.execute(
        "DELETE FROM race_results WHERE race_id IN (SELECT id FROM races WHERE regatta_id = ?)",
        (regatta_id,),
    )
    await db.execute("DELETE FROM races WHERE regatta_id = ?", (regatta_id,))
    await db.execute("DELETE FROM regattas WHERE id = ?", (regatta_id,))
    await db.commit()
    await audit(
        request,
        "results_regatta_delete",
        detail=json.dumps(
            {
                "regatta_id": regatta_id,
            }
        ),
    )
    return JSONResponse({"ok": True})


@router.post("/api/results/regattas/{regatta_id}/fetch", response_class=JSONResponse)
async def api_fetch_results(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    import httpx

    from helmlog.results.base import Regatta, get_provider
    from helmlog.results.importer import import_results

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT * FROM regattas WHERE id = ?", (regatta_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Regatta not found")

    provider = get_provider(row["source"])
    if not provider:
        raise HTTPException(400, f"No provider for source {row['source']!r}")

    regatta = Regatta(
        source=row["source"],
        source_id=row["source_id"],
        name=row["name"],
        url=row["url"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        default_class=row["default_class"],
        id=row["id"],
    )

    async with httpx.AsyncClient() as client:
        from helmlog.results.clubspot import ClubspotProvider
        from helmlog.results.styc import StycProvider

        providers_map: dict[str, type[ClubspotProvider] | type[StycProvider]] = {
            "clubspot": ClubspotProvider,
            "styc": StycProvider,
        }
        cls = providers_map.get(row["source"])
        if not cls:
            raise HTTPException(400, f"No provider class for {row['source']!r}")
        provider_instance = cls(client=client)
        results = await provider_instance.fetch(regatta)

    counts = await import_results(storage, results, user_id=_user.get("id"))
    return JSONResponse({"ok": True, **counts})


@router.post("/api/results/regattas/{regatta_id}/rematch", response_class=JSONResponse)
async def api_rematch_regatta(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Re-run local-session matching over an already-imported regatta's races.

    Pairs imported races to local race-type sessions by order within each
    venue-local date.  Only updates rows with ``local_session_id IS NULL``;
    manual links and prior matches are preserved.  Useful when local
    sessions were created after the import.
    """
    from helmlog.results.importer import _link_regatta_races_to_local_sessions

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM regattas WHERE id = ?", (regatta_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "Regatta not found")

    cur = await db.execute(
        "SELECT COUNT(*) FROM races"
        " WHERE regatta_id = ? AND source IS NOT NULL AND source != 'live'",
        (regatta_id,),
    )
    row = await cur.fetchone()
    races_checked = int(row[0]) if row else 0

    linked = await _link_regatta_races_to_local_sessions(db, regatta_id)
    await db.commit()

    await audit(
        request,
        "results_regatta_rematch",
        detail=json.dumps(
            {"regatta_id": regatta_id, "races_checked": races_checked, "linked": linked}
        ),
    )
    return JSONResponse({"ok": True, "races_checked": races_checked, "linked": linked})


@router.get("/api/results/races", response_class=JSONResponse)
async def api_list_results(
    request: Request,
    regatta_id: int | None = None,
    class_name: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()

    where = []
    params: list[str | int] = []
    if regatta_id is not None:
        where.append("r.regatta_id = ?")
        params.append(regatta_id)
    if class_name:
        where.append("r.event = ?")
        params.append(class_name)

    where_sql = " AND ".join(where) if where else "1=1"

    cur = await db.execute(
        f"SELECT r.id, r.name, r.date, r.event AS class_name, r.race_num, "  # noqa: S608
        f"r.regatta_id, r.local_session_id, "
        f"ls.slug AS session_slug, ls.name AS session_name, "
        f"(SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = r.id) AS result_count "
        f"FROM races r "
        f"LEFT JOIN races ls ON ls.id = r.local_session_id "
        f"WHERE {where_sql} AND r.source IS NOT NULL "
        f"ORDER BY r.date DESC, r.race_num, r.event",
        params,
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/races/{race_id}/results", response_class=JSONResponse)
async def api_race_results(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT rr.place, rr.points, rr.status_code, rr.finish_time, "
        "rr.elapsed_seconds, rr.corrected_seconds, rr.fleet, "
        "b.sail_number, b.name AS boat_name, b.skipper, b.yacht_club "
        "FROM race_results rr JOIN boats b ON rr.boat_id = b.id "
        "WHERE rr.race_id = ? ORDER BY rr.place",
        (race_id,),
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/series", response_class=JSONResponse)
async def api_series_standings(
    request: Request,
    regatta_id: int,
    class_name: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()

    where = ["sr.regatta_id = ?"]
    params: list[str | int] = [regatta_id]
    if class_name:
        where.append("sr.class = ?")
        params.append(class_name)

    where_sql = " AND ".join(where)
    cur = await db.execute(
        f"SELECT sr.*, b.sail_number, b.name AS boat_name, "  # noqa: S608
        f"b.skipper, b.yacht_club "
        f"FROM series_results sr JOIN boats b ON sr.boat_id = b.id "
        f"WHERE {where_sql} "
        f"ORDER BY sr.place_in_class NULLS LAST",
        params,
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/classes", response_class=JSONResponse)
async def api_list_classes(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT DISTINCT event FROM races "
        "WHERE regatta_id = ? AND event IS NOT NULL ORDER BY event",
        (regatta_id,),
    )
    rows = await cur.fetchall()
    return JSONResponse([r["event"] for r in rows])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _list_regattas(storage: Storage) -> list[dict[str, Any]]:
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM races rc WHERE rc.regatta_id = r.id) AS race_count, "
        "(SELECT COUNT(*) FROM race_results rr "
        "  JOIN races rc2 ON rr.race_id = rc2.id WHERE rc2.regatta_id = r.id) AS result_count "
        "FROM regattas r ORDER BY r.created_at DESC"
    )
    return [dict(row) for row in await cur.fetchall()]
