"""API tests for /api/moments (#662)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app

if TYPE_CHECKING:
    from pathlib import Path


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


async def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestCreateList:
    @pytest.mark.asyncio
    async def test_create_and_list(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            resp = await c.post(
                f"/api/sessions/{sid}/moments",
                json={"anchor_kind": "session", "subject": "Hello"},
            )
            assert resp.status_code == 201
            moment_id = resp.json()["id"]

            listing = await c.get(f"/api/sessions/{sid}/moments")
            data = listing.json()
            assert any(m["id"] == moment_id for m in data["moments"])

    @pytest.mark.asyncio
    async def test_rejects_bad_anchor_kind(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            resp = await c.post(
                f"/api/sessions/{sid}/moments",
                json={"anchor_kind": "bookmark"},
            )
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_session_404(self, storage: Storage) -> None:
        async with await _client(storage) as c:
            resp = await c.post("/api/sessions/9999/moments", json={"anchor_kind": "session"})
            assert resp.status_code == 404


class TestUpdateDelete:
    @pytest.mark.asyncio
    async def test_patch_subject(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            moment_id = (
                await c.post(
                    f"/api/sessions/{sid}/moments",
                    json={"anchor_kind": "session", "subject": "a"},
                )
            ).json()["id"]
            resp = await c.patch(f"/api/moments/{moment_id}", json={"subject": "b"})
            assert resp.status_code == 200
            assert resp.json()["subject"] == "b"

    @pytest.mark.asyncio
    async def test_delete(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            moment_id = (
                await c.post(
                    f"/api/sessions/{sid}/moments",
                    json={"anchor_kind": "session"},
                )
            ).json()["id"]
            resp = await c.delete(f"/api/moments/{moment_id}")
            assert resp.status_code == 204
            get_resp = await c.get(f"/api/moments/{moment_id}")
            assert get_resp.status_code == 404


class TestResolve:
    @pytest.mark.asyncio
    async def test_resolve_and_unresolve(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            moment_id = (
                await c.post(
                    f"/api/sessions/{sid}/moments",
                    json={"anchor_kind": "session"},
                )
            ).json()["id"]
            await c.post(
                f"/api/moments/{moment_id}/resolve",
                json={"resolution_summary": "done"},
            )
            m = (await c.get(f"/api/moments/{moment_id}")).json()
            assert m["resolved"] == 1
            assert m["resolution_summary"] == "done"

            await c.post(f"/api/moments/{moment_id}/unresolve", json={})
            m = (await c.get(f"/api/moments/{moment_id}")).json()
            assert m["resolved"] == 0


class TestCommentsOnMoment:
    @pytest.mark.asyncio
    async def test_create_comment_and_read(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            moment_id = (
                await c.post(
                    f"/api/sessions/{sid}/moments",
                    json={"anchor_kind": "session"},
                )
            ).json()["id"]
            resp = await c.post(
                f"/api/moments/{moment_id}/comments",
                json={"body": "nice lift"},
            )
            assert resp.status_code == 201
            m = (await c.get(f"/api/moments/{moment_id}")).json()
            assert any(c["body"] == "nice lift" for c in m["comments"])

    @pytest.mark.asyncio
    async def test_empty_comment_rejected(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            moment_id = (
                await c.post(
                    f"/api/sessions/{sid}/moments",
                    json={"anchor_kind": "session"},
                )
            ).json()["id"]
            resp = await c.post(f"/api/moments/{moment_id}/comments", json={"body": ""})
            assert resp.status_code == 422


class TestCounterparties:
    @pytest.mark.asyncio
    async def test_counterparties_typeahead(self, storage: Storage) -> None:
        sid = await _race(storage)
        async with await _client(storage) as c:
            await c.post(
                f"/api/sessions/{sid}/moments",
                json={"anchor_kind": "session", "counterparty": "Orca"},
            )
            resp = await c.get("/api/moments/counterparties")
            assert resp.status_code == 200
            assert "Orca" in resp.json()["counterparties"]


class TestLegacyPhotoShim:
    @pytest.mark.asyncio
    async def test_legacy_photo_upload_creates_moment_plus_attachment(
        self, storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
        sid = await _race(storage)
        async with await _client(storage) as c:
            files = {"file": ("x.jpg", b"\xff\xd8\xff\xe0jpegstub", "image/jpeg")}
            resp = await c.post(
                f"/api/sessions/{sid}/notes/photo",
                files=files,
                data={"ts": "2026-01-01T12:05:00+00:00"},
            )
            assert resp.status_code == 201
            body = resp.json()
            assert "id" in body and "photo_path" in body
            m = (await c.get(f"/api/moments/{body['id']}")).json()
            assert any(a["kind"] == "photo" for a in m["attachments"])
