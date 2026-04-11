"""Tests for the Vakaros admin web routes (#458 cycle 5)."""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage
else:
    from pathlib import Path  # noqa: TC003  # runtime needed by pytest fixture types


def _build_minimal_vkx_bytes(ts_ms: int = 1_700_000_000_000) -> bytes:
    header = bytes([0xFF, 0x05, 0, 0, 0, 0, 0, 0])
    payload = struct.pack(
        "<Qiifffffff",
        ts_ms,
        round(47.68 / 1e-7),
        round(-122.41 / 1e-7),
        1.0,
        math.radians(0.0),
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    )
    row = bytes([0x02]) + payload
    terminator = bytes([0xFE]) + struct.pack("<H", len(row))
    return header + row + terminator


@pytest.fixture
def inbox_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    inbox = tmp_path / "vakaros-inbox"
    inbox.mkdir()
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("VAKAROS_INBOX_DIR", str(inbox))
    return inbox


@pytest.mark.asyncio
async def test_admin_vakaros_page_lists_inbox_files_and_sessions(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "alpha.vkx").write_bytes(b"x")
    (inbox_path / "bravo.vkx").write_bytes(b"x")
    (inbox_path / "readme.txt").write_bytes(b"ignored")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/vakaros")

    assert resp.status_code == 200
    body = resp.text
    assert "alpha.vkx" in body
    assert "bravo.vkx" in body
    assert "readme.txt" not in body
    # Empty state for the sessions list
    assert "No Vakaros sessions" in body or "vakaros_sessions" in body.lower()


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_processes_valid_file(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "good.vkx").write_bytes(_build_minimal_vkx_bytes())

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "good.vkx"},
            follow_redirects=False,
        )

    # Expect a redirect back to the admin page (PRG pattern).
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("/admin/vakaros")

    # Original is gone, archived in processed/, DB has one row.
    assert not (inbox_path / "good.vkx").exists()
    assert (inbox_path / "processed" / "good.vkx").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_moves_malformed_file_to_failed(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "junk.vkx").write_bytes(b"\x00\x00\x00")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "junk.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert (inbox_path / "failed" / "junk.vkx").exists()
    assert (inbox_path / "failed" / "junk.vkx.err").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_rejects_path_traversal(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "../escape.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code == 400
