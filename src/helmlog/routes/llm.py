"""LLM transcript Q&A and callback-detection HTTP routes (#697).

Endpoints:

* ``GET  /api/llm/consent``                       — read consent state (viewer)
* ``POST /api/llm/consent``                       — admin acknowledges (admin)
* ``GET  /api/sessions/{rid}/llm/qa``             — Q&A history (viewer)
* ``POST /api/sessions/{rid}/llm/qa``             — ask a question (crew)
* ``POST /api/llm/qa/{qa_id}/save-as-moment``     — save answer as moment (crew)
* ``GET  /api/sessions/{rid}/llm/callbacks``      — list callbacks (viewer)
* ``POST /api/sessions/{rid}/llm/callbacks/run``  — admin re-run (admin)
* ``POST /api/llm/callbacks/{cb_id}/save-as-moment`` — save callback (crew)
* ``GET  /api/sessions/{rid}/llm/cost``           — current spend + caps state
* ``PUT  /api/sessions/{rid}/llm/caps``           — admin per-race caps

The LLM client is read from ``request.app.state.llm_client`` so tests can
inject a fake. ``_get_llm_client`` lazily constructs a real client from
env when nothing is bound — production startup binds one explicitly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from helmlog.auth import require_auth
from helmlog.llm_callback_job import run_for_race
from helmlog.llm_client import LLMClient, LLMConfig
from helmlog.llm_policy import check_can_query, get_effective_caps
from helmlog.llm_transcript import build_race_transcript_text, parse_relative_ts
from helmlog.routes._helpers import audit, get_storage

if TYPE_CHECKING:
    from helmlog.storage import Storage

router = APIRouter()


async def _resolve_relative_anchor(
    storage: Storage,
    race_id: int,
    ts: str | None,
) -> str | None:
    """Convert an ``MM:SS`` / ``H:MM:SS`` callback timestamp (relative to the
    first audio session of the race) into an absolute UTC ISO string suitable
    for ``moments.anchor_t_start``. Returns None if the timestamp can't be
    parsed or no audio session exists."""
    from datetime import timedelta

    if not ts:
        return None
    offset_s = parse_relative_ts(ts)
    if offset_s is None:
        return None
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT start_utc FROM audio_sessions WHERE race_id = ? ORDER BY start_utc, id LIMIT 1",
        (race_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    from datetime import datetime as _dt

    base = _dt.fromisoformat(row["start_utc"])
    return (base + timedelta(seconds=offset_s)).isoformat()


def _get_llm_client(request: Request) -> LLMClient:
    client: LLMClient | None = getattr(request.app.state, "llm_client", None)
    if client is not None:
        return client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM not configured (ANTHROPIC_API_KEY missing)",
        )
    cfg = LLMConfig(
        api_key=api_key,
        model=os.getenv("LLM_MODEL", "claude-sonnet-4-6"),
        endpoint=os.getenv("LLM_ENDPOINT", "https://api.anthropic.com/v1/messages"),
        input_usd_per_mtok=3.00,
        output_usd_per_mtok=15.00,
        cache_read_usd_per_mtok=0.30,
        cache_write_usd_per_mtok=3.75,
    )
    new_client = LLMClient(cfg)
    request.app.state.llm_client = new_client
    return new_client


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


@router.get("/api/llm/consent")
async def api_get_llm_consent(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    consent = await storage.get_llm_consent()
    if consent is None:
        return JSONResponse({"acknowledged": False, "by_user": None, "at": None})
    return JSONResponse({"acknowledged": True, **consent})


@router.post("/api/llm/consent")
async def api_acknowledge_llm_consent(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    logger.info("llm.consent.ack starting user_id={}", user.get("id"))
    try:
        await storage.acknowledge_llm_consent(user_id=user["id"])
        logger.info("llm.consent.ack stored")
    except Exception as exc:
        logger.exception("llm.consent.ack storage update failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"storage: {exc}") from exc
    try:
        await audit(request, "llm.consent.ack", user=user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm.consent.ack audit failed (non-fatal): {}", exc)
    consent = await storage.get_llm_consent()
    logger.info("llm.consent.ack returning consent={}", consent)
    return JSONResponse({"acknowledged": True, **(consent or {})})


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str
    confirm_cost: bool = False


@router.get("/api/sessions/{race_id}/llm/qa")
async def api_list_qa(
    request: Request,
    race_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    rows = await storage.list_llm_qa(race_id)
    return JSONResponse({"qa": rows})


@router.post("/api/sessions/{race_id}/llm/qa")
async def api_ask(
    request: Request,
    race_id: int,
    body: AskRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    if not body.question.strip():
        raise HTTPException(status_code=422, detail="question is required")

    build = await build_race_transcript_text(storage, race_id)
    if build is None:
        raise HTTPException(
            status_code=404,
            detail="No diarized transcript for this race",
        )
    transcript = build.text

    client = _get_llm_client(request)
    estimate = client.estimate_input_cost(transcript + body.question)

    check = await check_can_query(storage, race_id, estimate_usd=estimate)
    if not check.allowed:
        return JSONResponse(
            status_code=409 if check.reason == "consent_required" else 429,
            content={
                "reason": check.reason,
                "state": check.state.value,
                "current_spend_usd": check.current_spend_usd,
                "soft_warn_usd": check.soft_warn_usd,
                "hard_cap_usd": check.hard_cap_usd,
            },
        )
    if check.requires_confirmation and not body.confirm_cost:
        return JSONResponse(
            status_code=409,
            content={
                "reason": "confirmation_required",
                "state": check.state.value,
                "current_spend_usd": check.current_spend_usd,
                "soft_warn_usd": check.soft_warn_usd,
                "hard_cap_usd": check.hard_cap_usd,
            },
        )

    try:
        resp = await client.ask(transcript_text=transcript, question=body.question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM ask failed for race={}: {}", race_id, exc)
        await storage.insert_llm_qa(
            race_id=race_id,
            user_id=user["id"],
            question=body.question,
            answer=None,
            citations=[],
            model=getattr(client, "model", "?"),
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.0,
            status="failed",
            error_msg=str(exc),
        )
        raise HTTPException(status_code=502, detail="LLM provider error") from exc

    qa_id = await storage.insert_llm_qa(
        race_id=race_id,
        user_id=user["id"],
        question=body.question,
        answer=resp.text,
        citations=resp.citations,
        model=getattr(client, "model", "?"),
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cache_read_tokens=resp.cache_read_tokens,
        cache_create_tokens=resp.cache_create_tokens,
        cost_usd=resp.cost_usd,
    )
    await audit(request, "llm.qa.ask", detail=f"race={race_id} qa={qa_id}", user=user)
    return JSONResponse(
        {
            "id": qa_id,
            "answer": resp.text,
            "citations": resp.citations,
            "cost_usd": resp.cost_usd,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cache_read_tokens": resp.cache_read_tokens,
        }
    )


@router.post("/api/llm/qa/{qa_id}/save-as-moment", status_code=201)
async def api_qa_save_as_moment(
    request: Request,
    qa_id: int,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT race_id, question, answer, citations_json FROM llm_qa WHERE id = ?",
        (qa_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="qa not found")

    import json as _json

    citations: list[dict[str, Any]] = _json.loads(row["citations_json"] or "[]")
    subject = (row["question"] or "")[:200]

    if citations:
        first_ts = str(citations[0].get("ts") or "")
        anchor = await _resolve_relative_anchor(storage, row["race_id"], first_ts)
        if anchor is None:
            raise HTTPException(
                status_code=409,
                detail=f"could not resolve citation timestamp {first_ts!r}",
            )
        moment_id = await storage.create_moment(
            session_id=row["race_id"],
            anchor_kind="timestamp",
            anchor_t_start=anchor,
            subject=subject,
            source="llm",
            user_id=user["id"],
        )
    else:
        moment_id = await storage.create_moment(
            session_id=row["race_id"],
            anchor_kind="session",
            subject=subject,
            source="llm",
            user_id=user["id"],
        )

    await audit(
        request,
        "llm.qa.save_moment",
        detail=f"qa={qa_id} moment={moment_id}",
        user=user,
    )
    return JSONResponse({"moment_id": moment_id}, status_code=201)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{race_id}/llm/callbacks")
async def api_list_callbacks(
    request: Request,
    race_id: int,
    speaker: str | None = None,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    cbs = await storage.list_llm_callbacks(race_id, speaker=speaker)
    job = await storage.get_callback_job(race_id)
    return JSONResponse({"callbacks": cbs, "job": job})


@router.post("/api/sessions/{race_id}/llm/callbacks/run")
async def api_run_callback_detection(
    request: Request,
    race_id: int,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    client = _get_llm_client(request)
    result = await run_for_race(storage, race_id, client)
    if "skipped" in result:
        reason = result["skipped"]
        if reason == "no_transcript":
            raise HTTPException(status_code=404, detail="No diarized transcript")
        return JSONResponse(
            status_code=409 if reason == "consent_required" else 429,
            content={"reason": reason},
        )
    if "failed" in result:
        raise HTTPException(status_code=502, detail="LLM provider error")
    await audit(
        request,
        "llm.callbacks.run",
        detail=f"race={race_id} count={result['count']}",
        user=user,
    )
    return JSONResponse(result)


@router.post("/api/llm/callbacks/{cb_id}/save-as-moment", status_code=201)
async def api_callback_save_as_moment(
    request: Request,
    cb_id: int,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT id, race_id, anchor_ts, source_excerpt FROM llm_callbacks WHERE id = ?",
        (cb_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="callback not found")
    anchor = await _resolve_relative_anchor(storage, row["race_id"], row["anchor_ts"])
    if anchor is None:
        raise HTTPException(
            status_code=409,
            detail=f"could not resolve callback timestamp {row['anchor_ts']!r}",
        )
    moment_id = await storage.create_moment(
        session_id=row["race_id"],
        anchor_kind="timestamp",
        anchor_t_start=anchor,
        subject=row["source_excerpt"][:200],
        source="llm",
        user_id=user["id"],
    )
    await storage.link_llm_callback_moment(callback_id=cb_id, moment_id=moment_id)
    await audit(
        request,
        "llm.callback.save_moment",
        detail=f"cb={cb_id} moment={moment_id}",
        user=user,
    )
    return JSONResponse({"moment_id": moment_id}, status_code=201)


# ---------------------------------------------------------------------------
# Cost + caps
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{race_id}/llm/cost")
async def api_get_cost(
    request: Request,
    race_id: int,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    caps = await get_effective_caps(storage, race_id)
    spend = await storage.race_llm_cost(race_id)
    if spend >= caps.hard_cap_usd:
        state = "AtCap"
    elif spend >= caps.soft_warn_usd:
        state = "SoftWarned"
    else:
        state = "UnderSoft"
    return JSONResponse(
        {
            "current_spend_usd": spend,
            "soft_warn_usd": caps.soft_warn_usd,
            "hard_cap_usd": caps.hard_cap_usd,
            "state": state,
        }
    )


class CapsUpdate(BaseModel):
    soft_warn_usd: float | None = None
    hard_cap_usd: float | None = None


@router.put("/api/sessions/{race_id}/llm/caps")
async def api_set_caps(
    request: Request,
    race_id: int,
    body: CapsUpdate,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    await storage.set_race_caps(
        race_id=race_id,
        soft_warn_usd=body.soft_warn_usd,
        hard_cap_usd=body.hard_cap_usd,
        by_user=user["id"],
    )
    await audit(request, "llm.caps.set", detail=f"race={race_id}", user=user)
    return JSONResponse({"ok": True})
