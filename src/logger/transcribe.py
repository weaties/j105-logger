"""Audio transcription via faster-whisper.

Transcription runs in a thread pool to avoid blocking the event loop.
Results (including errors) are stored in the ``transcripts`` SQLite table.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from logger.storage import Storage


async def transcribe_session(
    storage: Storage,
    audio_session_id: int,
    transcript_id: int,
    model_size: str = "base",
) -> None:
    """Run faster-whisper transcription and update the transcript row.

    This coroutine is intended to be launched as a background task.
    *transcript_id* must be an existing row (status='pending') created by
    ``storage.create_transcript_job()``.
    """
    row = await storage.get_audio_session_row(audio_session_id)
    if row is None:
        await storage.update_transcript(
            transcript_id,
            status="error",
            error_msg=f"Audio session {audio_session_id} not found",
        )
        return

    await storage.update_transcript(transcript_id, status="running")
    file_path: str = row["file_path"]
    try:
        text = await asyncio.to_thread(_run_whisper, file_path=file_path, model_size=model_size)
        await storage.update_transcript(transcript_id, status="done", text=text)
        logger.info(
            "Transcription done: audio_session_id={} chars={}",
            audio_session_id,
            len(text),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed: audio_session_id={} err={}", audio_session_id, exc)
        await storage.update_transcript(transcript_id, status="error", error_msg=str(exc))


def _run_whisper(*, file_path: str, model_size: str) -> str:
    """Run faster-whisper synchronously (called from asyncio.to_thread)."""
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    logger.debug("Loading faster-whisper model={} for {}", model_size, file_path)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(file_path, beam_size=5)
    parts = [seg.text for seg in segments]
    return " ".join(parts).strip()
