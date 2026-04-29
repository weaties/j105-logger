"""Post-transcription LLM callback-detection job (#697 spec §2).

Two entry points:

* ``run_for_race`` — given a race id, build the transcript, run the LLM,
  persist the result, and update the lifecycle row. Used by the admin
  re-run endpoint and the auto-trigger.
* ``maybe_run_after_transcription`` — given a freshly-completed audio
  session id, look up the parent race and call ``run_for_race`` if there
  is one. This is the hook ``transcribe.py`` calls at every "done" path.
  No-op if the audio session isn't linked to a race.

Both gates collapse on ``check_can_query`` so consent + cost cap are
enforced consistently with the Q&A path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from helmlog.llm_policy import check_can_query
from helmlog.llm_transcript import build_race_transcript_text

if TYPE_CHECKING:
    from helmlog.storage import Storage


class CallbackDetector(Protocol):
    def estimate_input_cost(self, text: str) -> float: ...

    async def detect_callbacks(
        self,
        *,
        transcript_text: str,
    ) -> tuple[list[dict[str, Any]], float]: ...


async def run_for_race(
    storage: Storage,
    race_id: int,
    client: CallbackDetector,
) -> dict[str, Any]:
    """Run callback detection for one race and persist the result.

    Returns a dict describing the outcome:

      * ``{"skipped": "<reason>"}`` when the consent or cost-cap gate
        blocks the run (no LLM call was made).
      * ``{"count": N, "cost_usd": X}`` on success.
      * ``{"failed": "<error>"}`` when the LLM call raised.
    """
    build = await build_race_transcript_text(storage, race_id)
    if build is None:
        return {"skipped": "no_transcript"}
    transcript = build.text

    estimate = client.estimate_input_cost(transcript)
    check = await check_can_query(storage, race_id, estimate_usd=estimate)
    if not check.allowed:
        return {"skipped": check.reason or "blocked"}

    await storage.set_callback_job(race_id, status="Running")
    try:
        cbs, cost = await client.detect_callbacks(transcript_text=transcript)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM callback detection failed race={}: {}", race_id, exc)
        await storage.set_callback_job(race_id, status="Failed", error_msg=str(exc))
        return {"failed": str(exc)}

    rows = [
        {
            "speaker_label": cb.get("speaker"),
            "anchor_ts": cb.get("anchor_ts"),
            "source_excerpt": cb.get("excerpt", ""),
            "rationale": cb.get("rationale"),
        }
        for cb in cbs
        if cb.get("anchor_ts")
    ]
    await storage.replace_llm_callbacks(
        race_id=race_id,
        callbacks=rows,
        job_cost_usd=cost,
    )
    return {"count": len(rows), "cost_usd": cost}


async def maybe_run_after_transcription(
    storage: Storage,
    *,
    audio_session_id: int,
    client: CallbackDetector,
) -> dict[str, Any] | None:
    """Auto-trigger hook called from ``transcribe.py`` at every "done" path.

    Returns the run result, or None if the audio session isn't linked to
    a race (so the caller can log a debug-level "skipped" without noise).
    """
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT race_id FROM audio_sessions WHERE id = ?",
        (audio_session_id,),
    )
    row = await cur.fetchone()
    if row is None or row["race_id"] is None:
        return None
    return await run_for_race(storage, int(row["race_id"]), client)
