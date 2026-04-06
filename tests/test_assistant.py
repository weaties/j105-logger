"""Tests for the Claude-powered Q&A assistant (#429).

Covers:
- Source file selection logic (keyword matching)
- System prompt construction (CLAUDE.md, module index, relevant files)
- API route tests (auth gating, dev mode check, chat endpoint)
- Feature hidden when ANTHROPIC_API_KEY is unset
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helmlog.assistant import (
    build_module_index,
    build_system_prompt,
    is_configured,
    select_source_files,
)
from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Source file selection
# ---------------------------------------------------------------------------


class TestSelectSourceFiles:
    def test_polar_keywords(self) -> None:
        result = select_source_files("How does the polar baseline builder work?")
        assert "polar.py" in result

    def test_signal_k_keywords(self) -> None:
        result = select_source_files("Tell me about Signal K data")
        assert "sk_reader.py" in result

    def test_can_nmea_keywords(self) -> None:
        result = select_source_files("How do NMEA PGN decoders work?")
        assert "can_reader.py" in result
        assert "nmea2000.py" in result

    def test_federation_keywords(self) -> None:
        result = select_source_files("How does the co-op federation work?")
        assert "federation.py" in result

    def test_no_match_returns_empty(self) -> None:
        result = select_source_files("What is the meaning of life?")
        assert result == []

    def test_case_insensitive(self) -> None:
        result = select_source_files("POLAR performance")
        assert "polar.py" in result

    def test_multiple_keywords_deduped(self) -> None:
        result = select_source_files("How do federation and co-op peer sharing work?")
        assert "federation.py" in result
        # federation.py should appear only once
        assert result.count("federation.py") == 1

    def test_storage_keywords(self) -> None:
        result = select_source_files("How does the SQLite database schema work?")
        assert "storage.py" in result

    def test_transcription_keywords(self) -> None:
        result = select_source_files("How does transcription work?")
        assert "transcribe.py" in result

    def test_audio_keywords(self) -> None:
        result = select_source_files("Tell me about audio recording")
        assert "audio.py" in result


# ---------------------------------------------------------------------------
# Module index
# ---------------------------------------------------------------------------


class TestBuildModuleIndex:
    def test_returns_nonempty_string(self) -> None:
        index = build_module_index()
        assert len(index) > 0

    def test_contains_known_modules(self) -> None:
        index = build_module_index()
        assert "storage.py" in index
        assert "web.py" in index
        assert "auth.py" in index

    def test_excludes_dunder_files(self) -> None:
        index = build_module_index()
        assert "__init__.py" not in index


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_always_has_preamble(self) -> None:
        prompt = build_system_prompt("Hello")
        assert "helpful assistant" in prompt

    def test_includes_module_index(self) -> None:
        prompt = build_system_prompt("Hello")
        assert "Module Index" in prompt

    def test_includes_relevant_source(self) -> None:
        prompt = build_system_prompt("How does the polar module work?")
        assert "polar.py" in prompt

    def test_no_source_for_generic_question(self) -> None:
        prompt = build_system_prompt("What is your name?")
        assert "Relevant Source Code" not in prompt


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_not_configured_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert is_configured() is False

    def test_configured_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        assert is_configured() is True

    def test_not_configured_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert is_configured() is False


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

_AUTH_ENV = {"AUTH_DISABLED": "false"}


async def _create_admin_user(storage: Storage, *, is_developer: bool = True) -> tuple[int, str]:
    """Create an admin user and return (user_id, session_id)."""
    user_id = await storage.create_user(
        "admin@test.com", "Test Admin", "admin", is_developer=is_developer
    )
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_viewer_user(storage: Storage) -> tuple[int, str]:
    """Create a viewer user and return (user_id, session_id)."""
    user_id = await storage.create_user("viewer@test.com", "Viewer", "viewer")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


class TestAssistantRoutes:
    """Test access control for assistant endpoints."""

    @pytest.mark.asyncio
    async def test_page_requires_auth(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get("/admin/assistant", follow_redirects=False)
                assert r.status_code in (307, 401)

    @pytest.mark.asyncio
    async def test_page_requires_developer(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage, is_developer=False)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    "/admin/assistant",
                    cookies={"session": session_id},
                    follow_redirects=False,
                )
                assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_page_requires_api_key(self, storage: Storage) -> None:
        with patch.dict(os.environ, _AUTH_ENV, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    "/admin/assistant",
                    cookies={"session": session_id},
                    follow_redirects=False,
                )
                assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_page_accessible_for_admin_developer(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    "/admin/assistant",
                    cookies={"session": session_id},
                )
                assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_chat_requires_auth(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
                assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_chat_requires_developer(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage, is_developer=False)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    cookies={"session": session_id},
                )
                assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_chat_no_api_key_returns_503(self, storage: Storage) -> None:
        with patch.dict(os.environ, _AUTH_ENV, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    cookies={"session": session_id},
                )
                assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_chat_viewer_forbidden(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_viewer_user(storage)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    cookies={"session": session_id},
                )
                assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_chat_empty_messages_422(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": []},
                    cookies={"session": session_id},
                )
                assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_success(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)

            mock_chat = AsyncMock(return_value="The polar module builds performance baselines.")
            with patch("helmlog.routes.assistant.assistant_chat", mock_chat):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    r = await client.post(
                        "/api/assistant/chat",
                        json={
                            "messages": [
                                {"role": "user", "content": "How does the polar module work?"}
                            ]
                        },
                        cookies={"session": session_id},
                    )
                    assert r.status_code == 200
                    data = r.json()
                    assert data["role"] == "assistant"
                    assert "polar" in data["content"].lower()

    @pytest.mark.asyncio
    async def test_chat_too_many_messages_422(self, storage: Storage) -> None:
        with patch.dict(os.environ, {**_AUTH_ENV, "ANTHROPIC_API_KEY": "sk-test"}):
            _, session_id = await _create_admin_user(storage)
            app = create_app(storage)
            messages = [{"role": "user", "content": f"msg {i}"} for i in range(51)]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/assistant/chat",
                    json={"messages": messages},
                    cookies={"session": session_id},
                )
                assert r.status_code == 422
