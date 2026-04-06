"""Route handlers for the Claude-powered Q&A assistant (#429)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel

from helmlog.assistant import chat as assistant_chat
from helmlog.assistant import is_configured
from helmlog.auth import require_auth, require_developer
from helmlog.routes._helpers import templates, tpl_ctx

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


@router.get("/admin/assistant", response_class=HTMLResponse, include_in_schema=False)
async def admin_assistant_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    _dev: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> Response:
    if not is_configured():
        raise HTTPException(status_code=404, detail="Assistant not configured")
    return templates.TemplateResponse(
        request, "admin/assistant.html", tpl_ctx(request, "/admin/assistant")
    )


@router.post("/api/assistant/chat")
async def api_assistant_chat(
    request: Request,
    body: ChatRequest,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    _dev: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    if not is_configured():
        raise HTTPException(status_code=503, detail="Assistant not configured")

    if not body.messages:
        raise HTTPException(status_code=422, detail="Messages must not be empty")

    if len(body.messages) > 50:
        raise HTTPException(status_code=422, detail="Conversation too long")

    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    try:
        content = await assistant_chat(messages)
    except Exception:
        logger.exception("Assistant API error")
        raise HTTPException(status_code=502, detail="Assistant API error") from None

    return JSONResponse({"role": "assistant", "content": content})
