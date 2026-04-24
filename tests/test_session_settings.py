"""Tests for session_settings (config snapshots rescued from session_notes)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _race(storage: Storage) -> int:
    r = await storage.start_race(
        "T",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "2026-01-01",
        1,
        "race-1",
        "race",
    )
    return r.id  # type: ignore[return-value]


class TestStorage:
    @pytest.mark.asyncio
    async def test_create_and_list(self, storage: Storage) -> None:
        sid = await _race(storage)
        await storage.create_session_setting(
            session_id=sid,
            body='{"backstay": 5}',
        )
        rows = await storage.list_session_settings(sid)
        assert len(rows) == 1
        assert "backstay" in rows[0]["body"]

    @pytest.mark.asyncio
    async def test_keys_union_across_rows(self, storage: Storage) -> None:
        sid = await _race(storage)
        await storage.create_session_setting(
            session_id=sid,
            body='{"backstay": 5}',
        )
        await storage.create_session_setting(
            session_id=sid,
            body='{"jib_lead": 3}',
        )
        keys = await storage.list_settings_keys()
        assert keys == ["backstay", "jib_lead"]

    @pytest.mark.asyncio
    async def test_ignores_non_object_bodies(self, storage: Storage) -> None:
        sid = await _race(storage)
        await storage.create_session_setting(session_id=sid, body="[1,2]")
        await storage.create_session_setting(session_id=sid, body="not json")
        assert await storage.list_settings_keys() == []


class TestAPI:
    @pytest.mark.asyncio
    async def test_create_and_keys(self, storage: Storage) -> None:
        sid = await _race(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                f"/api/sessions/{sid}/settings",
                json={"body": '{"tws": 15, "twd": 220}'},
            )
            assert resp.status_code == 201
            keys = (await c.get("/api/session-settings/keys")).json()
            assert "tws" in keys["keys"]

    @pytest.mark.asyncio
    async def test_rejects_non_object_body(self, storage: Storage) -> None:
        sid = await _race(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                f"/api/sessions/{sid}/settings",
                json={"body": "[1,2]"},
            )
            assert resp.status_code == 422
