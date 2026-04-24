"""Storage + API tests for moment attachments (#662)."""

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


async def _moment(storage: Storage, session_id: int) -> int:
    return await storage.create_moment(session_id=session_id, anchor_kind="session")


class TestAttachmentStorage:
    @pytest.mark.asyncio
    async def test_create_list_delete(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await _moment(storage, sid)
        aid = await storage.create_attachment(
            moment_id=mid,
            kind="note",
            body="Scratchpad",
        )
        attachments = await storage.list_attachments_for_moment(mid)
        assert len(attachments) == 1
        assert attachments[0]["id"] == aid
        assert attachments[0]["kind"] == "note"
        assert attachments[0]["body"] == "Scratchpad"
        assert await storage.delete_attachment(aid) is True
        assert await storage.list_attachments_for_moment(mid) == []

    @pytest.mark.asyncio
    async def test_delete_with_file_returns_path(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await _moment(storage, sid)
        aid = await storage.create_attachment(
            moment_id=mid,
            kind="photo",
            path="1/photo.jpg",
        )
        found, path = await storage.delete_attachment_with_file(aid)
        assert found is True
        assert path == "1/photo.jpg"

    @pytest.mark.asyncio
    async def test_cascade_on_moment_delete(self, storage: Storage) -> None:
        sid = await _race(storage)
        mid = await _moment(storage, sid)
        await storage.create_attachment(moment_id=mid, kind="photo", path="x")
        # Deleting the parent moment removes the attachment via FK CASCADE.
        await storage.delete_moment(mid)
        assert await storage.list_attachments_for_moment(mid) == []


class TestAttachmentAPI:
    @pytest.mark.asyncio
    async def test_upload_photo(
        self, storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
        sid = await _race(storage)
        mid = await _moment(storage, sid)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            files = {"file": ("x.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")}
            resp = await c.post(f"/api/moments/{mid}/attachments", files=files)
            assert resp.status_code == 201
            data = resp.json()
            assert data["kind"] == "photo"
            assert data["path"].startswith(f"{sid}/")
            assert (tmp_path / data["path"]).exists()

    @pytest.mark.asyncio
    async def test_delete_removes_file(
        self, storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
        sid = await _race(storage)
        mid = await _moment(storage, sid)
        (tmp_path / "x.jpg").write_bytes(b"\xff\xd8")
        aid = await storage.create_attachment(
            moment_id=mid,
            kind="photo",
            path="x.jpg",
        )
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.delete(f"/api/attachments/{aid}")
            assert resp.status_code == 204
            assert not (tmp_path / "x.jpg").exists()
