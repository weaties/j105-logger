"""Unit tests for logger.transcribe — _merge and diarisation helpers."""

from __future__ import annotations

from logger.transcribe import _merge


def test_merge_assigns_speaker_by_midpoint() -> None:
    """Midpoint of a whisper segment falling in speaker A's interval → SPEAKER_00."""
    whisper_segs = [(0.0, 3.0, "Ready about.")]
    diar_segs = [(0.0, 3.5, "A"), (3.5, 7.0, "B")]
    result = _merge(whisper_segs, diar_segs)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "Ready about."
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 3.0


def test_merge_normalises_labels() -> None:
    """Two unique raw speaker labels map to SPEAKER_00 and SPEAKER_01."""
    whisper_segs = [(0.0, 3.0, "Hello."), (3.5, 6.0, "World.")]
    diar_segs = [(0.0, 3.2, "X"), (3.3, 6.5, "Y")]
    result = _merge(whisper_segs, diar_segs)
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[1]["speaker"] == "SPEAKER_01"


def test_merge_empty_diarizer() -> None:
    """With no diarisation intervals all segments fall back to SPEAKER_00."""
    whisper_segs = [(0.0, 2.0, "One."), (2.1, 4.0, "Two.")]
    result = _merge(whisper_segs, [])
    assert all(seg["speaker"] == "SPEAKER_00" for seg in result)
    assert len(result) == 2


def test_merge_returns_all_fields() -> None:
    """Each returned dict has start, end, speaker, and text keys."""
    whisper_segs = [(1.0, 2.5, "Tack!")]
    diar_segs = [(0.5, 3.0, "A")]
    result = _merge(whisper_segs, diar_segs)
    assert set(result[0].keys()) == {"start", "end", "speaker", "text"}


def test_merge_nearest_fallback() -> None:
    """Midpoint outside all diarisation intervals uses nearest interval."""
    # whisper segment at 10–12 s; diar covers 0–3 only
    whisper_segs = [(10.0, 12.0, "Late.")]
    diar_segs = [(0.0, 3.0, "A")]
    result = _merge(whisper_segs, diar_segs)
    # Nearest is the only interval; should still be normalised SPEAKER_00
    assert result[0]["speaker"] == "SPEAKER_00"
