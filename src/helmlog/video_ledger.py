"""Persistent record of which Insta360 recordings have already been uploaded.

The ledger is a small JSON file keyed by ``(volume_uuid, source_filename,
size_bytes)`` so the pipeline can skip recordings that have already made
it to YouTube even if the SD card is re-inserted, the camera is plugged
in twice, or a previous run was interrupted after upload but before
linking.

The file lives at ``~/.config/helmlog/video-ledger.json`` by default.
A single ledger is shared across all cameras — entries are
camera-distinguished by ``volume_uuid``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_ledger_path() -> Path:
    """Return the default ledger location."""
    return Path.home() / ".config" / "helmlog" / "video-ledger.json"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerKey:
    """Identifies a recording uniquely across re-mounts and renames."""

    volume_uuid: str
    source_filename: str
    size_bytes: int

    def as_str(self) -> str:
        return f"{self.volume_uuid}|{self.source_filename}|{self.size_bytes}"


@dataclass(frozen=True)
class LedgerEntry:
    """One row in the ledger."""

    volume_uuid: str
    source_filename: str
    size_bytes: int
    video_id: str
    youtube_url: str
    camera_label: str = ""
    session_id: int | None = None
    linked: bool = False


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


class VideoLedger:
    """Tiny JSON-backed ledger of uploaded recordings."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_ledger_path()
        self._entries: dict[str, LedgerEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read video ledger {}: {}", self.path, exc)
            return
        for row in raw.get("entries", []):
            try:
                entry = LedgerEntry(**row)
            except TypeError as exc:
                logger.warning("Skipping malformed ledger row: {}", exc)
                continue
            self._entries[
                LedgerKey(entry.volume_uuid, entry.source_filename, entry.size_bytes).as_str()
            ] = entry

    def has(self, key: LedgerKey) -> bool:
        return key.as_str() in self._entries

    def get(self, key: LedgerKey) -> LedgerEntry | None:
        return self._entries.get(key.as_str())

    def record(self, entry: LedgerEntry) -> None:
        """Add or update an entry and atomically rewrite the file."""
        key = LedgerKey(entry.volume_uuid, entry.source_filename, entry.size_bytes)
        self._entries[key.as_str()] = entry
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [asdict(e) for e in self._entries.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._entries)
