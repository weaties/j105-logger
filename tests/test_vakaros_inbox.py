"""Tests for the Vakaros inbox helpers (#458 cycle 5)."""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage
else:
    from pathlib import Path  # noqa: TC003  # runtime needed by pytest fixture types


def _build_minimal_vkx_bytes(ts_ms: int = 1_700_000_000_000) -> bytes:
    """Build a tiny valid VKX buffer with one Position row."""
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
def inbox(tmp_path: Path) -> Path:
    p = tmp_path / "vakaros-inbox"
    p.mkdir()
    return p


@pytest.mark.asyncio
async def test_list_inbox_files_returns_only_vkx_and_ignores_subdirs(inbox: Path) -> None:
    from helmlog.vakaros_inbox import list_inbox_files

    (inbox / "foo.vkx").write_bytes(b"x")
    (inbox / "bar.VKX").write_bytes(b"x")  # case-insensitive
    (inbox / "readme.txt").write_bytes(b"x")
    (inbox / "processed").mkdir()
    (inbox / "processed" / "old.vkx").write_bytes(b"x")

    files = list_inbox_files(inbox)
    names = sorted(f.name for f in files)
    assert names == ["bar.VKX", "foo.vkx"]


@pytest.mark.asyncio
async def test_list_inbox_files_creates_missing_directory(tmp_path: Path) -> None:
    from helmlog.vakaros_inbox import list_inbox_files

    missing = tmp_path / "never-existed"
    files = list_inbox_files(missing)
    assert files == []
    assert missing.is_dir()
    assert (missing / "processed").is_dir()
    assert (missing / "failed").is_dir()


@pytest.mark.asyncio
async def test_ingest_inbox_file_moves_to_processed_on_success(
    storage: Storage, inbox: Path
) -> None:
    from helmlog.vakaros_inbox import ingest_inbox_file

    vkx = inbox / "session_a.vkx"
    vkx.write_bytes(_build_minimal_vkx_bytes())

    result = await ingest_inbox_file(storage, inbox, "session_a.vkx")
    assert result.session_id > 0
    assert result.status == "ingested"
    assert result.archived_path is not None
    assert result.archived_path.name == "session_a.vkx"
    assert result.archived_path.parent == inbox / "processed"
    assert result.archived_path.exists()
    assert not vkx.exists()


@pytest.mark.asyncio
async def test_ingest_inbox_file_marks_duplicate_without_reinserting(
    storage: Storage, inbox: Path
) -> None:
    from helmlog.vakaros_inbox import ingest_inbox_file

    buf = _build_minimal_vkx_bytes()
    (inbox / "first.vkx").write_bytes(buf)
    r1 = await ingest_inbox_file(storage, inbox, "first.vkx")
    assert r1.status == "ingested"

    # Copy the identical bytes to a new filename and ingest again.
    (inbox / "second.vkx").write_bytes(buf)
    r2 = await ingest_inbox_file(storage, inbox, "second.vkx")
    assert r2.session_id == r1.session_id
    assert r2.status == "duplicate"
    assert r2.archived_path is not None
    assert r2.archived_path.parent == inbox / "processed"


@pytest.mark.asyncio
async def test_ingest_inbox_file_moves_to_failed_on_parse_error(
    storage: Storage, inbox: Path
) -> None:
    from helmlog.vakaros_inbox import ingest_inbox_file

    bad = inbox / "corrupt.vkx"
    bad.write_bytes(b"\x00\x00\x00\x00\x00")  # nonsense

    result = await ingest_inbox_file(storage, inbox, "corrupt.vkx")
    assert result.status == "failed"
    assert result.session_id is None
    assert result.error is not None
    assert result.archived_path is not None
    assert result.archived_path.parent == inbox / "failed"
    assert result.archived_path.exists()
    err_sidecar = result.archived_path.with_suffix(result.archived_path.suffix + ".err")
    assert err_sidecar.exists()
    assert (
        "unknown" in err_sidecar.read_text().lower() or "parse" in err_sidecar.read_text().lower()
    )


@pytest.mark.asyncio
async def test_ingest_inbox_file_rejects_path_traversal(
    storage: Storage, inbox: Path, tmp_path: Path
) -> None:
    from helmlog.vakaros_inbox import ingest_inbox_file

    # Try to escape the inbox with a ../ filename.
    with pytest.raises(ValueError, match="outside inbox"):
        await ingest_inbox_file(storage, inbox, "../escape.vkx")


@pytest.mark.asyncio
async def test_ingest_inbox_file_rejects_missing_file(storage: Storage, inbox: Path) -> None:
    from helmlog.vakaros_inbox import ingest_inbox_file

    with pytest.raises(FileNotFoundError):
        await ingest_inbox_file(storage, inbox, "nonexistent.vkx")
