"""Vakaros VKX inbox helpers (#458).

Explicit, user-controlled ingest path: the user copies a .vkx file into
the inbox directory, then clicks "Ingest" in the admin UI. No background
watcher — each ingest is a discrete, logged action.

Directory layout (relative to VAKAROS_INBOX_DIR):
    <inbox>/               .vkx files waiting to be ingested
    <inbox>/processed/     successfully ingested files, preserved for audit
    <inbox>/failed/        files that failed to parse, with a .err sidecar
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

DEFAULT_INBOX_REL = "data/vakaros-inbox"


IngestStatus = Literal["ingested", "duplicate", "failed"]


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a single inbox ingest attempt."""

    filename: str
    status: IngestStatus
    session_id: int | None
    archived_path: Path | None
    error: str | None


def get_inbox_dir() -> Path:
    """Return the configured Vakaros inbox directory.

    Reads ``VAKAROS_INBOX_DIR`` from the environment, falling back to
    ``data/vakaros-inbox`` (relative to cwd).  The directory and its
    ``processed``/``failed`` subdirs are created on demand by the helpers
    that actually read or write to them — this function does not touch
    the filesystem.
    """
    env = os.environ.get("VAKAROS_INBOX_DIR")
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_INBOX_REL)


def _ensure_layout(inbox: Path) -> None:
    """Create inbox + processed/failed subdirs if they don't exist."""
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "processed").mkdir(exist_ok=True)
    (inbox / "failed").mkdir(exist_ok=True)


def list_inbox_files(inbox: Path) -> list[Path]:
    """Return .vkx files directly in the inbox (not subdirs), sorted by name.

    Ensures the inbox layout exists as a side effect.  Matching is
    case-insensitive on the `.vkx` suffix.
    """
    _ensure_layout(inbox)
    return sorted(
        (p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".vkx"),
        key=lambda p: p.name,
    )


def _resolve_safe_inbox_path(inbox: Path, filename: str) -> Path:
    """Resolve `<inbox>/<filename>` and reject any path-traversal attempt."""
    target = (inbox / filename).resolve()
    inbox_resolved = inbox.resolve()
    try:
        target.relative_to(inbox_resolved)
    except ValueError as exc:
        raise ValueError(f"filename {filename!r} resolves outside inbox") from exc
    return target


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    """Return a non-colliding destination path in `dest_dir`."""
    target = dest_dir / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}.{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


async def ingest_inbox_file(storage: Storage, inbox: Path, filename: str) -> IngestResult:
    """Parse, store, and archive a single inbox file.

    On parse failure, the file is moved to `failed/` with a `.err` sidecar
    and `IngestResult.status == "failed"`. On success it moves to
    `processed/`. Duplicate content (same SHA-256 hash) is also moved to
    `processed/` with `status == "duplicate"`.

    Raises:
        ValueError: if `filename` resolves outside the inbox
          (path-traversal protection).
        FileNotFoundError: if the file doesn't exist in the inbox.
    """
    from helmlog.vakaros import VKXParseError, ingest_vkx_file

    _ensure_layout(inbox)
    src = _resolve_safe_inbox_path(inbox, filename)
    if not src.is_file():
        raise FileNotFoundError(f"no such file in inbox: {filename}")

    try:
        session_id, was_duplicate = await ingest_vkx_file(storage, src)
    except VKXParseError as exc:
        failed_dir = inbox / "failed"
        archived = _unique_destination(failed_dir, src.name)
        src.rename(archived)
        err_text = f"{exc}\n\n{traceback.format_exc()}"
        archived.with_suffix(archived.suffix + ".err").write_text(err_text)
        logger.warning("Vakaros ingest failed for {}: {}", src.name, exc)
        return IngestResult(
            filename=filename,
            status="failed",
            session_id=None,
            archived_path=archived,
            error=str(exc),
        )

    processed_dir = inbox / "processed"
    archived = _unique_destination(processed_dir, src.name)
    src.rename(archived)
    status: IngestStatus = "duplicate" if was_duplicate else "ingested"
    logger.info("Vakaros inbox {}: session_id={} file={}", status, session_id, src.name)
    return IngestResult(
        filename=filename,
        status=status,
        session_id=session_id,
        archived_path=archived,
        error=None,
    )
