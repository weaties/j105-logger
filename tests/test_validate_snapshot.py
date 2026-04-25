"""Tests for scripts/validate_snapshot.py — backup completeness guard (#676)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "scripts" / "validate_snapshot.py"

spec = importlib.util.spec_from_file_location("validate_snapshot", _SCRIPT)
assert spec is not None and spec.loader is not None
validate_snapshot = importlib.util.module_from_spec(spec)
sys.modules["validate_snapshot"] = validate_snapshot
spec.loader.exec_module(validate_snapshot)


def _make_db(data_root: Path) -> None:
    db_path = data_root / "logger.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE moment_attachments (
            id INTEGER PRIMARY KEY, moment_id INTEGER, kind TEXT, path TEXT
        );
        CREATE TABLE audio_sessions (
            id INTEGER PRIMARY KEY, file_path TEXT
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, avatar_path TEXT
        );
        """
    )
    db.commit()
    db.close()


def test_all_present(tmp_path: Path) -> None:
    _make_db(tmp_path)
    (tmp_path / "notes" / "108").mkdir(parents=True)
    (tmp_path / "notes" / "108" / "a.jpg").write_bytes(b"x")
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute(
        "INSERT INTO moment_attachments(moment_id, kind, path) VALUES (1, 'photo', ?)",
        ("108/a.jpg",),
    )
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 0
    (ma,) = [r for r in reports if r.kind == "moment_attachments"]
    assert ma.total == 1 and ma.missing == 0


def test_missing_attachment_reported(tmp_path: Path) -> None:
    _make_db(tmp_path)
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute(
        "INSERT INTO moment_attachments(moment_id, kind, path) VALUES (1, 'photo', ?)",
        ("108/missing.jpg",),
    )
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 1
    (ma,) = [r for r in reports if r.kind == "moment_attachments"]
    assert ma.missing == 1
    assert "108/missing.jpg" in ma.samples


def test_absent_tables_tolerated(tmp_path: Path) -> None:
    """Older backups may lack audio_sessions / users tables — validator must
    not crash; those checks return 0/0 and overall status stays OK."""
    db_path = tmp_path / "logger.db"
    db = sqlite3.connect(db_path)
    db.executescript("CREATE TABLE moment_attachments (id INTEGER PRIMARY KEY, path TEXT);")
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 0
    kinds = {r.kind for r in reports}
    assert {"moment_attachments", "audio_sessions", "users.avatar_path"} == kinds


def test_missing_db_returns_rc_2(tmp_path: Path) -> None:
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 2
    assert reports == []


@pytest.mark.parametrize("abs_path", [True, False])
def test_audio_absolute_and_relative(tmp_path: Path, abs_path: bool) -> None:
    _make_db(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = audio_dir / "race42.wav"
    wav.write_bytes(b"riff")
    stored = str(wav) if abs_path else "audio/race42.wav"
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute("INSERT INTO audio_sessions(file_path) VALUES (?)", (stored,))
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    (a,) = [r for r in reports if r.kind == "audio_sessions"]
    assert a.total == 1 and a.missing == 0
    assert rc == 0
