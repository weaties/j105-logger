"""Tests for v63 multi-channel audio schema (#493 / #462 pt.1).

Pure storage layer: channel_map CRUD, transcript_segments per-channel rows,
and voice-consent acknowledgement audit entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


class TestSchemaV63:
    async def test_schema_at_v63_or_higher(self, storage: Storage) -> None:
        db = storage._conn()
        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        assert row is not None and row[0] is not None
        assert row[0] >= 63

    async def test_channel_map_table_exists(self, storage: Storage) -> None:
        db = storage._conn()
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_map'"
        )
        assert await cur.fetchone() is not None

    async def test_transcript_segments_table_exists(self, storage: Storage) -> None:
        db = storage._conn()
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transcript_segments'"
        )
        assert await cur.fetchone() is not None

    async def test_transcript_segments_has_channel_columns(self, storage: Storage) -> None:
        db = storage._conn()
        cur = await db.execute("PRAGMA table_info(transcript_segments)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "channel_index" in cols
        assert "position_name" in cols
        assert "transcript_id" in cols
        assert "start_time" in cols
        assert "end_time" in cols
        assert "text" in cols


# ---------------------------------------------------------------------------
# channel_map CRUD
# ---------------------------------------------------------------------------


class TestChannelMap:
    DEVICE = {
        "vendor_id": 0x1234,
        "product_id": 0x5678,
        "serial": "ABC123",
        "usb_port_path": "1-1.2",
    }

    async def test_set_and_get_default_map(self, storage: Storage) -> None:
        await storage.set_channel_map(
            **self.DEVICE,
            mapping={0: "helm", 1: "tactician", 2: "trim", 3: "bow"},
        )
        result = await storage.get_channel_map(**self.DEVICE)
        assert result == {0: "helm", 1: "tactician", 2: "trim", 3: "bow"}

    async def test_get_returns_empty_when_unset(self, storage: Storage) -> None:
        result = await storage.get_channel_map(**self.DEVICE)
        assert result == {}

    async def test_set_replaces_existing_default(self, storage: Storage) -> None:
        await storage.set_channel_map(**self.DEVICE, mapping={0: "helm", 1: "trim"})
        await storage.set_channel_map(**self.DEVICE, mapping={0: "tactician", 1: "bow"})
        result = await storage.get_channel_map(**self.DEVICE)
        assert result == {0: "tactician", 1: "bow"}

    async def test_session_override_falls_back_to_default(self, storage: Storage) -> None:
        # Need a real audio session for FK
        session_id = await self._make_audio_session(storage)
        await storage.set_channel_map(**self.DEVICE, mapping={0: "helm", 1: "bow"})
        # No override yet → fallback to default
        result = await storage.get_channel_map(**self.DEVICE, audio_session_id=session_id)
        assert result == {0: "helm", 1: "bow"}

    async def test_session_override_takes_precedence(self, storage: Storage) -> None:
        session_id = await self._make_audio_session(storage)
        await storage.set_channel_map(**self.DEVICE, mapping={0: "helm", 1: "bow"})
        await storage.set_channel_map(
            **self.DEVICE,
            mapping={0: "tactician", 1: "trim"},
            audio_session_id=session_id,
        )
        # Default unchanged
        assert await storage.get_channel_map(**self.DEVICE) == {0: "helm", 1: "bow"}
        # Override visible for that session
        assert await storage.get_channel_map(**self.DEVICE, audio_session_id=session_id) == {
            0: "tactician",
            1: "trim",
        }

    async def test_devices_with_different_ports_are_independent(self, storage: Storage) -> None:
        await storage.set_channel_map(**self.DEVICE, mapping={0: "helm"})
        other = {**self.DEVICE, "usb_port_path": "1-1.3"}
        await storage.set_channel_map(**other, mapping={0: "bow"})
        assert await storage.get_channel_map(**self.DEVICE) == {0: "helm"}
        assert await storage.get_channel_map(**other) == {0: "bow"}

    @staticmethod
    async def _make_audio_session(storage: Storage) -> int:
        from datetime import UTC
        from datetime import datetime as _dt

        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, sample_rate, channels)"
            " VALUES (?, ?, ?, ?, ?)",
            ("/tmp/x.wav", "TestMic", _dt.now(UTC).isoformat(), 48000, 4),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# transcript_segments round-trip
# ---------------------------------------------------------------------------


class TestTranscriptSegments:
    async def test_insert_and_list_segments(self, storage: Storage) -> None:
        transcript_id = await self._make_transcript(storage)
        segments = [
            {
                "segment_index": 0,
                "start_time": 0.0,
                "end_time": 1.5,
                "text": "ready about",
                "speaker": "helm",
                "channel_index": 0,
                "position_name": "helm",
            },
            {
                "segment_index": 1,
                "start_time": 1.5,
                "end_time": 2.0,
                "text": "trim on",
                "speaker": "trim",
                "channel_index": 2,
                "position_name": "trim",
            },
        ]
        await storage.insert_transcript_segments(transcript_id, segments)
        rows = await storage.list_transcript_segments(transcript_id)
        assert len(rows) == 2
        assert rows[0]["text"] == "ready about"
        assert rows[0]["channel_index"] == 0
        assert rows[0]["position_name"] == "helm"
        assert rows[1]["channel_index"] == 2
        assert rows[1]["position_name"] == "trim"

    async def test_segments_cascade_with_transcript(self, storage: Storage) -> None:
        transcript_id = await self._make_transcript(storage)
        await storage.insert_transcript_segments(
            transcript_id,
            [
                {
                    "segment_index": 0,
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "text": "x",
                    "speaker": None,
                    "channel_index": 0,
                    "position_name": "helm",
                }
            ],
        )
        db = storage._conn()
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))
        await db.commit()
        rows = await storage.list_transcript_segments(transcript_id)
        assert rows == []

    @staticmethod
    async def _make_transcript(storage: Storage) -> int:
        from datetime import UTC
        from datetime import datetime as _dt

        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, sample_rate, channels)"
            " VALUES (?, ?, ?, ?, ?)",
            ("/tmp/x.wav", "TestMic", _dt.now(UTC).isoformat(), 48000, 4),
        )
        await db.commit()
        audio_session_id = cur.lastrowid
        return await storage.create_transcript_job(audio_session_id, "tiny")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Voice-biometric consent acknowledgement audit
# ---------------------------------------------------------------------------


class TestAudioSessionDeviceIdentity:
    """v64: vendor/product/serial/usb_port_path columns on audio_sessions."""

    async def test_audio_sessions_has_device_identity_columns(self, storage: Storage) -> None:
        db = storage._conn()
        cur = await db.execute("PRAGMA table_info(audio_sessions)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "vendor_id" in cols
        assert "product_id" in cols
        assert "serial" in cols
        assert "usb_port_path" in cols

    async def test_set_and_read_device_identity(self, storage: Storage) -> None:
        from datetime import UTC
        from datetime import datetime as _dt

        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, sample_rate, channels)"
            " VALUES (?, ?, ?, ?, ?)",
            ("/tmp/x.wav", "Lavalier4", _dt.now(UTC).isoformat(), 48000, 4),
        )
        await db.commit()
        session_id = cur.lastrowid
        assert session_id is not None

        await storage.set_audio_session_device(
            session_id,
            vendor_id=0x1234,
            product_id=0x5678,
            serial="ABC",
            usb_port_path="1-1.2",
        )
        row = await storage.get_audio_session_row(session_id)
        assert row is not None
        assert row["vendor_id"] == 0x1234
        assert row["product_id"] == 0x5678
        assert row["serial"] == "ABC"
        assert row["usb_port_path"] == "1-1.2"

    async def test_get_channel_map_via_session_device(self, storage: Storage) -> None:
        """Once a session has identity, channel_map lookup chains through it."""
        from datetime import UTC
        from datetime import datetime as _dt

        device = {
            "vendor_id": 0x1234,
            "product_id": 0x5678,
            "serial": "ABC",
            "usb_port_path": "1-1.2",
        }
        await storage.set_channel_map(**device, mapping={0: "helm", 1: "trim"})

        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, sample_rate, channels)"
            " VALUES (?, ?, ?, ?, ?)",
            ("/tmp/x.wav", "Lavalier4", _dt.now(UTC).isoformat(), 48000, 4),
        )
        await db.commit()
        session_id = cur.lastrowid
        await storage.set_audio_session_device(session_id, **device)  # type: ignore[arg-type]

        result = await storage.get_channel_map_for_audio_session(session_id)
        assert result == {0: "helm", 1: "trim"}


class TestVoiceConsentAudit:
    async def test_log_voice_consent_writes_audit_entry(self, storage: Storage) -> None:
        await storage.log_voice_consent_ack(
            user_id=None,
            position_name="helm",
            device={
                "vendor_id": 0x1234,
                "product_id": 0x5678,
                "serial": "ABC123",
                "usb_port_path": "1-1.2",
            },
        )
        entries = await storage.list_audit_log(limit=10)
        assert any(e["action"] == "voice_consent_ack" for e in entries)
        entry = next(e for e in entries if e["action"] == "voice_consent_ack")
        assert "helm" in (entry["detail"] or "")
