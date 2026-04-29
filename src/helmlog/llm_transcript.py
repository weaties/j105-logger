"""Transcript-text builder for LLM prompts (#697).

Renders the diarized segments of a race's audio sessions as
``[MM:SS] speaker: text`` (or ``[H:MM:SS]`` past one hour) lines, with
the time origin pinned to the **first** audio session's ``start_utc``.

This format matches what the user already sees in the transcript pane,
so citations the LLM emits ``[MM:SS]`` map cleanly back to seek offsets
on the audio player. ``parse_relative_ts`` is the inverse, used by
the save-as-moment route to anchor saved moments at the right absolute
UTC time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from helmlog.storage import Storage


class TranscriptBuild(NamedTuple):
    text: str
    audio_session_id: int
    audio_start_utc: datetime


_RELATIVE_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def parse_relative_ts(ts: str) -> int | None:
    """Parse ``MM:SS`` or ``H:MM:SS`` to seconds. Returns None on bad input."""
    m = _RELATIVE_TS_RE.match(ts.strip())
    if m is None:
        return None
    h_str, mm_str, ss_str = m.groups()
    h = int(h_str) if h_str is not None else 0
    mm = int(mm_str)
    ss = int(ss_str)
    if mm >= 60 or ss >= 60:
        return None
    return h * 3600 + mm * 60 + ss


def _format_offset(seconds: float) -> str:
    s = int(round(seconds))
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


async def build_race_transcript_text(
    storage: Storage,
    race_id: int,
) -> TranscriptBuild | None:
    """Return ``(text, audio_session_id, audio_start_utc)`` for a race.

    The text is encoded as ``[MM:SS]`` lines relative to the first audio
    session's start. Returns None if the race has no diarized transcript
    (route handler should 404). Segments from later audio sessions are
    folded into the same time origin so a 35-minute race + 10-minute
    dock chat reads as ``[0:00]…[44:30]``.
    """
    race = await storage.get_race(race_id)
    if race is None or race.start_utc is None:
        return None

    db = storage._read_conn()
    cur = await db.execute(
        "SELECT a.id AS audio_id, a.start_utc AS audio_start,"
        " t.segments_json, t.speaker_map"
        " FROM audio_sessions a JOIN transcripts t ON t.audio_session_id = a.id"
        " WHERE a.race_id = ? AND t.status = 'done'"
        " ORDER BY a.start_utc, a.id",
        (race_id,),
    )
    rows = await cur.fetchall()
    if not rows:
        return None

    base_audio_id: int | None = None
    base_audio_start: datetime | None = None
    lines: list[str] = []
    for row in rows:
        if not row["segments_json"]:
            continue
        try:
            segs: list[dict[str, Any]] = json.loads(row["segments_json"])
        except json.JSONDecodeError:
            continue
        # speaker_map is {raw_label: {"type": "crew"|"auto", "name": str, ...}}
        speaker_map: dict[str, dict[str, Any]] = {}
        if row["speaker_map"]:
            try:
                speaker_map = json.loads(row["speaker_map"]) or {}
            except json.JSONDecodeError:
                speaker_map = {}
        audio_start = datetime.fromisoformat(row["audio_start"])
        if base_audio_start is None:
            base_audio_start = audio_start
            base_audio_id = int(row["audio_id"])
        for seg in segs:
            start_s = float(seg.get("start", 0) or 0)
            absolute = audio_start + timedelta(seconds=start_s)
            offset_s = (absolute - base_audio_start).total_seconds()
            raw_label = str(seg.get("speaker") or seg.get("position_name") or "?")
            mapped = speaker_map.get(raw_label) or {}
            speaker = str(mapped.get("name") or raw_label)
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            lines.append(f"[{_format_offset(offset_s)}] {speaker}: {text}")

    if not lines or base_audio_start is None or base_audio_id is None:
        return None
    return TranscriptBuild(
        text="\n".join(lines),
        audio_session_id=base_audio_id,
        audio_start_utc=base_audio_start,
    )
