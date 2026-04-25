#!/usr/bin/env python3
"""Validate that every on-disk artifact referenced by logger.db exists.

Issue #676: backup snapshots silently lose photo/audio/avatar files when
the DB is copied but the attachment tree isn't (or when paths in the DB
point outside the backed-up root). This script cross-checks:

  moment_attachments.path  → <data_root>/notes/<path>
  audio_sessions.file_path → absolute, or <data_root>/audio/<path>
  users.avatar_path        → <data_root>/avatars/<path>

Usage:
  python3 scripts/validate_snapshot.py <data_root>
    data_root should contain logger.db plus the notes/, audio/,
    avatars/ subdirectories (i.e. the equivalent of ~/helmlog/data/
    on the Pi, or <snapshot>/data/ in a backup tree).

Exit codes:
  0   all referenced files present
  1   one or more orphaned DB rows (missing files)
  2   could not open the DB or data root
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OrphanReport:
    kind: str
    total: int
    missing: int
    samples: list[str]


def _check_attachments(db: sqlite3.Connection, notes_root: Path) -> OrphanReport:
    try:
        cur = db.execute(
            "SELECT path FROM moment_attachments WHERE path IS NOT NULL AND path != ''"
        )
    except sqlite3.OperationalError:
        # Pre-moments schema, or a non-standard test DB.
        return OrphanReport("moment_attachments", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (path,) in rows:
        full = notes_root / str(path)
        if not full.is_file():
            missing.append(str(path))
    return OrphanReport("moment_attachments", len(rows), len(missing), missing[:5])


def _check_audio(db: sqlite3.Connection, data_root: Path) -> OrphanReport:
    # file_path is typically absolute (/home/weaties/helmlog/data/audio/...)
    # but older rows may be relative to the helmlog CWD.
    try:
        cur = db.execute(
            "SELECT file_path FROM audio_sessions WHERE file_path IS NOT NULL AND file_path != ''"
        )
    except sqlite3.OperationalError:
        # Pre-audio schema
        return OrphanReport("audio_sessions", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (fp,) in rows:
        p = Path(fp)
        candidates = [p] if p.is_absolute() else [data_root / p, data_root / "audio" / p.name]
        if not any(c.is_file() for c in candidates):
            missing.append(str(fp))
    return OrphanReport("audio_sessions", len(rows), len(missing), missing[:5])


def _check_avatars(db: sqlite3.Connection, avatars_root: Path) -> OrphanReport:
    try:
        cur = db.execute(
            "SELECT avatar_path FROM users WHERE avatar_path IS NOT NULL AND avatar_path != ''"
        )
    except sqlite3.OperationalError:
        return OrphanReport("users.avatar_path", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (ap,) in rows:
        p = Path(ap)
        full = p if p.is_absolute() else avatars_root / p
        if not full.is_file():
            missing.append(str(ap))
    return OrphanReport("users.avatar_path", len(rows), len(missing), missing[:5])


def validate(data_root: Path) -> tuple[int, list[OrphanReport]]:
    db_path = data_root / "logger.db"
    if not db_path.is_file():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 2, []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        reports = [
            _check_attachments(db, data_root / "notes"),
            _check_audio(db, data_root),
            _check_avatars(db, data_root / "avatars"),
        ]
    finally:
        db.close()

    worst = 0 if all(r.missing == 0 for r in reports) else 1
    return worst, reports


def _format_report(reports: list[OrphanReport]) -> str:
    lines = []
    for r in reports:
        pct = f"{(r.total - r.missing) / r.total * 100:.1f}%" if r.total else "n/a"
        status = "OK" if r.missing == 0 else f"MISSING {r.missing}"
        lines.append(f"- {r.kind}: {r.total} rows, {status} ({pct} present)")
        for s in r.samples:
            lines.append(f"    missing: {s}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("data_root", type=Path, help="Directory containing logger.db")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Only print output on failure or when orphans are found",
    )
    args = p.parse_args()

    if not args.data_root.is_dir():
        print(f"ERROR: {args.data_root} is not a directory", file=sys.stderr)
        return 2

    rc, reports = validate(args.data_root)
    if rc == 0 and args.quiet:
        return 0
    header = "Snapshot validation: " + ("OK" if rc == 0 else "ORPHANED ROWS FOUND")
    print(header)
    print(_format_report(reports))
    return rc


if __name__ == "__main__":
    sys.exit(main())
