"""Transcript-text builder for LLM prompts (#697).

Joins ``races → audio_sessions → transcripts`` and renders the diarized
segments as ``[HH:MM:SS] speaker: text`` lines anchored to the race's
absolute UTC start. The prompt-cached transcript portion of each Q&A is
just the string this function returns, so it must be deterministic for
cache hits across questions in the same race session.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def build_race_transcript_text(storage: Storage, race_id: int) -> str | None:
    """Return the diarized transcript for a race, or None if none exists.

    None means the route handler should 404 — there's nothing to ask
    questions about.
    """
    race = await storage.get_race(race_id)
    if race is None:
        return None
    race_start: datetime | None = race.start_utc
    if race_start is None:
        return None

    db = storage._read_conn()
    cur = await db.execute(
        "SELECT a.id AS audio_id, a.start_utc AS audio_start, t.segments_json"
        " FROM audio_sessions a JOIN transcripts t ON t.audio_session_id = a.id"
        " WHERE a.race_id = ? AND t.status = 'done'"
        " ORDER BY a.start_utc",
        (race_id,),
    )
    rows = await cur.fetchall()
    if not rows:
        return None

    lines: list[str] = []
    for row in rows:
        if not row["segments_json"]:
            continue
        try:
            segs: list[dict[str, Any]] = json.loads(row["segments_json"])
        except json.JSONDecodeError:
            continue
        audio_start = datetime.fromisoformat(row["audio_start"])
        for seg in segs:
            start_s = float(seg.get("start", 0) or 0)
            ts = audio_start + timedelta(seconds=start_s)
            speaker = str(seg.get("speaker") or seg.get("position_name") or "?")
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            lines.append(f"[{ts.strftime('%H:%M:%S')}] {speaker}: {text}")

    if not lines:
        return None
    return "\n".join(lines)
