#!/usr/bin/env bash
# import-matched.sh — Copy session-matching Insta360 recordings off the SD
# card into a clean staging directory for processing.
#
# Walks every VID_*.mp4 / .insv on the card, parses the timestamp from the
# filename, probes the actual video duration with ffprobe, then asks the
# HelmLog Pi which races/practices/debriefs that recording overlaps. Only
# files that overlap a real session get copied — random non-session footage
# (drone shots, dock loading, kid videos) is ignored so the import dir
# stays clean and small.
#
# Usage:
#   ./scripts/import-matched.sh                    # auto-detect SD card
#   ./scripts/import-matched.sh /Volumes/Sailing360
#
# Environment overrides:
#   HELMLOG_IMPORT_DIR    where to copy matches  (default: ~/Insta360 Imports)
#   PI_API_URL            HelmLog API            (default: http://corvopi-live:3002)
#   PI_SESSION_COOKIE     Pi auth cookie         (REQUIRED — fetch fails open
#                          but then nothing matches and nothing is copied)
#   TIMEZONE              camera local TZ        (default: America/Los_Angeles)
#
# Idempotent: re-running skips files that are already in the import dir.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

IMPORT_DIR="${HELMLOG_IMPORT_DIR:-$HOME/Insta360 Imports}"
PI_API="${PI_API_URL:-http://corvopi-live:3002}"
COOKIE="${PI_SESSION_COOKIE:-}"
TZ_NAME="${TIMEZONE:-America/Los_Angeles}"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# ── Locate the SD card ───────────────────────────────────────────────────
find_mount() {
  if [ -n "${1:-}" ] && [ -d "$1/DCIM/Camera01" ]; then
    echo "$1"
    return 0
  fi
  for vol in /Volumes/*; do
    if [ -d "$vol/DCIM/Camera01" ]; then
      echo "$vol"
      return 0
    fi
  done
  return 1
}

SD_MOUNT=$(find_mount "${1:-}") || {
  log "No Insta360 SD card mounted — nothing to import."
  exit 0
}
log "SD card: $SD_MOUNT"
log "Import dir: $IMPORT_DIR"
log "Pi API: $PI_API"
mkdir -p "$IMPORT_DIR"

if [ -z "$COOKIE" ]; then
  log "WARN: PI_SESSION_COOKIE not set — session fetch will return zero, nothing will be copied."
fi

# ── Match + copy via Python (reuses pipeline modules) ─────────────────────
cd "$PROJECT_DIR"

PI_API_URL="$PI_API" PI_SESSION_COOKIE="$COOKIE" TIMEZONE="$TZ_NAME" \
HELMLOG_SD_MOUNT="$SD_MOUNT" HELMLOG_IMPORT_DIR="$IMPORT_DIR" \
uv run --no-sync python << 'PY'
import asyncio
import os
import shutil
from datetime import timedelta
from pathlib import Path

from loguru import logger

from helmlog.insta360 import (
    discover_recordings,
    match_sessions,
    probe_duration_s,
    recording_start_utc,
)
from helmlog.pipeline import fetch_sessions_from_pi


async def main() -> int:
    sd = Path(os.environ["HELMLOG_SD_MOUNT"])
    import_dir = Path(os.environ["HELMLOG_IMPORT_DIR"])
    pi_url = os.environ["PI_API_URL"]
    cookie = os.environ.get("PI_SESSION_COOKIE", "")
    tz = os.environ.get("TIMEZONE", "America/Los_Angeles")

    sessions = await fetch_sessions_from_pi(pi_url, session_cookie=cookie)
    print(f"fetched {len(sessions)} session(s) from Pi")
    if not sessions:
        print("nothing to match against — exiting")
        return 0

    recordings = discover_recordings(sd)
    print(f"discovered {len(recordings)} recording(s) on SD card")

    copied = 0
    skipped_existing = 0
    skipped_unmatched = 0

    for rec in recordings:
        start = recording_start_utc(rec, tz)

        # Probe the actual duration so the matching window is real, not the
        # old start+2h heuristic. Falls back to 2h on probe failure (won't
        # under-match).
        first = rec.segments[0] if rec.segments else None
        duration = probe_duration_s(first) if first is not None else None
        if duration is None:
            duration = 7200.0
        end = start + timedelta(seconds=duration)

        matched = match_sessions(start, end, sessions)
        if matched is None:
            skipped_unmatched += 1
            continue

        # Walk debrief → parent race the same way the upload pipeline does, so
        # the import decision and the link decision agree on which session
        # this recording belongs to.
        parent_id = matched.get("parent_race_id")
        target = matched
        if parent_id is not None:
            for s in sessions:
                if s.get("id") == parent_id and (
                    s.get("type") == "race" or s.get("session_type") == "race"
                ):
                    target = s
                    break

        name = target.get("name", target.get("id"))
        print(
            f"  [{rec.timestamp_str}] {len(rec.segments)} seg, "
            f"{rec.total_size_bytes / 1_073_741_824:.2f} GB → {name}"
        )

        # Copy every segment of this recording. Multi-segment recordings
        # (Studio joins them on export) need all parts present in the import
        # dir before Studio can stitch them together.
        for seg in rec.segments:
            dest = import_dir / seg.name
            if dest.exists() and dest.stat().st_size == seg.stat().st_size:
                skipped_existing += 1
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            print(f"    copying {seg.name} ({seg.stat().st_size / 1_073_741_824:.2f} GB)")
            shutil.copy2(seg, tmp)
            tmp.replace(dest)
            copied += 1

        # Also copy the LRV preview if it exists — Studio uses it for fast
        # scrubbing and it's tiny relative to the main file.
        for seg in rec.segments:
            lrv = seg.with_name(seg.name.replace("VID_", "LRV_").replace("_00_", "_01_"))
            if lrv.exists():
                lrv_dest = import_dir / lrv.name
                if not lrv_dest.exists():
                    shutil.copy2(lrv, lrv_dest)

    print()
    print(
        f"summary: {copied} segment(s) copied, "
        f"{skipped_existing} already-present, "
        f"{skipped_unmatched} unmatched recording(s) skipped"
    )
    return 0


import sys

sys.exit(asyncio.run(main()))
PY
