#!/usr/bin/env bash
# process-videos.sh — Automated Insta360 X4 video pipeline.
#
# Discovers .insv files on a mounted SD card, stitches them into
# equirectangular 360° MP4 via Docker + insta360-cli-utils, injects
# spatial metadata, uploads to YouTube, and links to HelmLog sessions.
#
# Usage:
#   ./scripts/process-videos.sh                    # auto-detect SD card
#   ./scripts/process-videos.sh /Volumes/MyCard    # explicit mount point
#
# Called by com.helmlog.video.plist (launchd) when a volume is mounted,
# or run manually after inserting the SD card.
#
# Environment overrides:
#   VIDEO_OUTPUT_DIR          base dir for stitched MP4s (default: ~/Insta360 Exports)
#                              Each camera's videos go in <VIDEO_OUTPUT_DIR>/<camera_label>/
#   VIDEO_RESOLUTION          output resolution        (default: 3840x1920)
#   VIDEO_BITRATE             output bitrate           (e.g. 100M; default: stitcher default)
#   VIDEO_FLOWSTATE           FlowState stabilization  (default: true)
#   VIDEO_DIRECTION_LOCK      FlowState direction lock (default: true)
#   DOCKER_IMAGE              stitcher image           (default: insta360-cli-utils)
#   PI_API_URL                HelmLog API              (default: http://corvopi:3002)
#   VIDEO_PRIVACY             YouTube privacy          (default: unlisted)
#   TIMEZONE                  camera local timezone    (default: America/Los_Angeles)
#   YOUTUBE_ACCOUNT           YouTube channel handle   (default: corvo105)
#                              Selects ~/.config/helmlog/youtube/<account>.json
#   YOUTUBE_CLIENT_SECRETS    OAuth2 client secrets    (default: ~/.helmlog-youtube-client-secrets.json)
#   PI_SESSION_COOKIE         session cookie for Pi API (enables auto-linking videos to sessions)
#
# Multi-camera: when invoked without arguments the script processes EVERY
# mounted Insta360 volume in parallel. Pass an explicit mount path to
# process just one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configuration ────────────────────────────────────────────────────────────

OUTPUT_BASE="${VIDEO_OUTPUT_DIR:-$HOME/Insta360 Exports}"
RESOLUTION="${VIDEO_RESOLUTION:-3840x1920}"
BITRATE="${VIDEO_BITRATE:-}"
FLOWSTATE="${VIDEO_FLOWSTATE:-true}"
DIRECTION_LOCK="${VIDEO_DIRECTION_LOCK:-true}"
IMAGE="${DOCKER_IMAGE:-insta360-cli-utils}"
PI_API="${PI_API_URL:-http://corvopi:3002}"
PRIVACY="${VIDEO_PRIVACY:-unlisted}"
TZ_NAME="${TIMEZONE:-America/Los_Angeles}"
YT_ACCOUNT="${YOUTUBE_ACCOUNT:-corvo105}"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
warn() { echo "[$(date -u +%H:%M:%SZ)] WARNING: $*" >&2; }
die() { echo "[$(date -u +%H:%M:%SZ)] ERROR: $*" >&2; exit 1; }

# ── 1. Locate the Insta360 SD card ──────────────────────────────────────────

find_insta360_mounts() {
  # Explicit argument takes priority
  if [ -n "${1:-}" ] && [ -d "$1/DCIM/Camera01" ]; then
    echo "$1"
    return 0
  fi
  local found=0
  for vol in /Volumes/*; do
    if [ -d "$vol/DCIM/Camera01" ]; then
      if ls "$vol"/DCIM/Camera01/VID_*.insv &>/dev/null || ls "$vol"/DCIM/Camera01/VID_*.mp4 &>/dev/null; then
        echo "$vol"
        found=1
      fi
    fi
  done
  [ "$found" = "1" ]
}

MOUNTS=()
while IFS= read -r line; do
  [ -n "$line" ] && MOUNTS+=("$line")
done < <(find_insta360_mounts "${1:-}" || true)

if [ ${#MOUNTS[@]} -eq 0 ]; then
  log "No Insta360 SD card detected — nothing to do."
  exit 0
fi

log "Insta360 volume(s) found: ${MOUNTS[*]}"

# Resolve a camera label per mount via volume UUID lookup
resolve_camera_label_for() {
  local mount="$1"
  cd "$PROJECT_DIR" && uv run --no-sync python -c "
from pathlib import Path
from helmlog.insta360 import resolve_camera_label
label, _known = resolve_camera_label(Path('$mount'))
print(label)
" 2>/dev/null || echo "$(basename "$mount")"
}

# When multiple volumes are mounted, recurse one process per volume so each
# camera processes in parallel without sharing temp dirs.
if [ ${#MOUNTS[@]} -gt 1 ] && [ -z "${HELMLOG_PIPELINE_CHILD:-}" ]; then
  log "Fanning out ${#MOUNTS[@]} pipeline runs (one per camera)..."
  pids=()
  for m in "${MOUNTS[@]}"; do
    HELMLOG_PIPELINE_CHILD=1 "$0" "$m" &
    pids+=($!)
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || warn "child pipeline pid $pid exited non-zero"
  done
  exit 0
fi

SD_MOUNT="${MOUNTS[0]}"
CAMERA_LABEL="$(resolve_camera_label_for "$SD_MOUNT")"
OUTPUT_DIR="$OUTPUT_BASE/$CAMERA_LABEL"
log "Processing $SD_MOUNT as camera label: $CAMERA_LABEL"
log "Output dir: $OUTPUT_DIR"

# ── 2. Confirmation dialog (only when triggered by launchd) ─────────────────

# If stdin is not a terminal (launchd trigger), show a macOS dialog
if ! [ -t 0 ]; then
  RESPONSE=$(osascript -e "
    display dialog \"Insta360 X4 detected at $SD_MOUNT.\" & return & return & \
    \"Process and upload race videos?\" \
    buttons {\"Cancel\", \"Process\"} default button \"Process\" \
    with title \"HelmLog Video Pipeline\"
  " 2>/dev/null) || {
    log "User cancelled — exiting."
    exit 0
  }
fi

# ── 3. Discover recordings ──────────────────────────────────────────────────

log "Discovering recordings..."
RECORDINGS=$(cd "$PROJECT_DIR" && uv run python -c "
import json
from pathlib import Path
from helmlog.insta360 import discover_recordings
recs = discover_recordings(Path('$SD_MOUNT'))
print(json.dumps([
    {
        'timestamp': r.timestamp_str,
        'segments': [str(s) for s in r.segments],
        'size': r.total_size_bytes,
        'needs_stitching': r.needs_stitching,
    }
    for r in recs
]))
")

REC_COUNT=$(echo "$RECORDINGS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$REC_COUNT" -eq 0 ]; then
  log "No recordings found on SD card."
  exit 0
fi

log "Found $REC_COUNT recording(s)"

# ── 4. Stitch / copy each recording ───────────────────────────────────────

mkdir -p "$OUTPUT_DIR"

# Clean up any stale pending-uploads from a previous interrupted run
rm -f "$OUTPUT_DIR/.pending_uploads"

# Check if any recording needs stitching (Docker required only for .insv)
NEEDS_DOCKER=$(echo "$RECORDINGS" | python3 -c "
import sys, json
recs = json.load(sys.stdin)
print('yes' if any(r.get('needs_stitching') for r in recs) else 'no')
")

if [ "$NEEDS_DOCKER" = "yes" ] && ! docker info &>/dev/null; then
  die "Docker is required for 360° stitching but is not running. Start Docker Desktop and try again."
fi

echo "$RECORDINGS" | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    segs = ' '.join(r['segments'])
    stitch = '1' if r.get('needs_stitching') else '0'
    print(f\"{r['timestamp']}|{stitch}|{segs}\")
" | while IFS='|' read -r TS NEEDS_STITCH SEGS; do
  OUTPUT_FILE="$OUTPUT_DIR/${TS}.mp4"

  if [ -f "$OUTPUT_FILE" ]; then
    log "  [$TS] Already processed → $OUTPUT_FILE (skipping)"
  elif [ "$NEEDS_STITCH" = "1" ]; then
    # 360° .insv — stitch via Docker
    log "  [$TS] Stitching (360°)..."
    CAMERA_DIR="$SD_MOUNT/DCIM/Camera01"

    INPUT_ARGS=""
    for seg in $SEGS; do
      BASENAME=$(basename "$seg")
      INPUT_ARGS="$INPUT_ARGS /input/$BASENAME"
    done

    STITCH_FLAGS=()
    if [ "$FLOWSTATE" = "true" ]; then
      STITCH_FLAGS+=(--flowstate)
    else
      STITCH_FLAGS+=(--no-flowstate)
    fi
    if [ "$DIRECTION_LOCK" = "true" ]; then
      STITCH_FLAGS+=(--direction-lock)
    else
      STITCH_FLAGS+=(--no-direction-lock)
    fi
    [ -n "$BITRATE" ] && STITCH_FLAGS+=(--bitrate "$BITRATE")
    [ -n "$RESOLUTION" ] && STITCH_FLAGS+=(--resolution "$RESOLUTION")

    docker run --rm \
      -v "$CAMERA_DIR:/input:ro" \
      -v "$OUTPUT_DIR:/output" \
      "$IMAGE" \
      "${STITCH_FLAGS[@]}" \
      --output "/output/${TS}.mp4" $INPUT_ARGS \
    || {
      warn "[$TS] Stitching failed — skipping this recording"
      continue
    }

    log "  [$TS] Stitched → $OUTPUT_FILE"
  else
    # Single-lens .mp4 — copy directly (no stitching needed)
    log "  [$TS] Copying (single-lens)..."
    FIRST_SEG=$(echo "$SEGS" | awk '{print $1}')

    if [ "$(echo "$SEGS" | wc -w)" -eq 1 ]; then
      cp "$FIRST_SEG" "$OUTPUT_FILE"
    else
      # Multiple segments — concatenate with ffmpeg
      CONCAT_FILE=$(mktemp "$OUTPUT_DIR/.concat_XXXXXX.txt")
      for seg in $SEGS; do
        echo "file '$seg'" >> "$CONCAT_FILE"
      done
      ffmpeg -y -f concat -safe 0 -i "$CONCAT_FILE" \
        -c copy -movflags +faststart "$OUTPUT_FILE" 2>/dev/null \
      || {
        warn "[$TS] Concatenation failed — skipping"
        rm -f "$CONCAT_FILE"
        continue
      }
      rm -f "$CONCAT_FILE"
    fi

    log "  [$TS] Copied → $OUTPUT_FILE"
  fi

  echo "$TS $OUTPUT_FILE" >> "$OUTPUT_DIR/.pending_uploads"
done

# ── 5. Upload to YouTube + link to sessions ─────────────────────────────────

if [ ! -f "$OUTPUT_DIR/.pending_uploads" ]; then
  log "No new videos to upload."
  exit 0
fi

log "Uploading to YouTube and linking to sessions..."

export HELMLOG_CAMERA_LABEL="$CAMERA_LABEL"
export YOUTUBE_ACCOUNT="$YT_ACCOUNT"
export VIDEO_PRIVACY="$PRIVACY"
export PI_API_URL="$PI_API"

cd "$PROJECT_DIR"
uv run python -c "
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from helmlog.insta360 import InstaRecording
from helmlog.pipeline import PipelineConfig, fetch_sessions_from_pi, process_recording

async def main():
    cfg = PipelineConfig(
        pi_api_url=os.environ.get('PI_API_URL', 'http://corvopi:3002'),
        pi_session_cookie=os.environ.get('PI_SESSION_COOKIE', ''),
        privacy=os.environ.get('VIDEO_PRIVACY', 'unlisted'),
        timezone=os.environ.get('TIMEZONE', 'America/Los_Angeles'),
        camera_label=os.environ.get('HELMLOG_CAMERA_LABEL', ''),
        youtube_account=os.environ.get('YOUTUBE_ACCOUNT', ''),
    )

    # Fetch sessions from the Pi for matching
    sessions = await fetch_sessions_from_pi(
        cfg.pi_api_url, session_cookie=cfg.pi_session_cookie,
    )
    print(f'  Fetched {len(sessions)} sessions from Pi')

    results = []
    with open('$OUTPUT_DIR/.pending_uploads') as f:
        for line in f:
            ts, filepath = line.strip().split(' ', 1)
            filepath = Path(filepath)
            if not filepath.exists():
                print(f'  [{ts}] File not found: {filepath}', file=sys.stderr)
                continue

            rec = InstaRecording(timestamp_str=ts, segments=[], total_size_bytes=0)
            result = await process_recording(
                rec=rec,
                video_path=filepath,
                sessions=sessions,
                config=cfg,
            )

            entry = {
                'timestamp': ts,
                'video_id': result.video_id,
                'youtube_url': result.youtube_url,
                'session_id': result.session_id,
                'linked': result.linked,
                'uploaded': result.uploaded,
            }
            if result.uploaded:
                print(f'  [{ts}] Uploaded → {result.youtube_url}')
                if result.linked:
                    print(f'  [{ts}] Linked to session {result.session_id}')
                elif result.session_id and not cfg.pi_session_cookie:
                    print(f'  [{ts}] Skipping link — set PI_SESSION_COOKIE', file=sys.stderr)
            else:
                print(f'  [{ts}] Failed: {result.error}', file=sys.stderr)
            results.append(entry)

    uploaded = [r for r in results if r['uploaded']]
    if uploaded:
        with open('$OUTPUT_DIR/.upload_results.json', 'w') as f:
            json.dump(uploaded, f, indent=2)
        linked = sum(1 for r in uploaded if r.get('linked'))
        print(f'  {len(uploaded)} video(s) uploaded, {linked} linked to sessions')

asyncio.run(main())
"

# Clean up pending file
rm -f "$OUTPUT_DIR/.pending_uploads"

# ── 5b. Clean up stitched files after upload (#211 — data policy) ──────────

CLEANUP="${VIDEO_CLEANUP_AFTER_UPLOAD:-false}"
if [ "$CLEANUP" = "true" ] && [ -f "$OUTPUT_DIR/.upload_results.json" ]; then
  log "Cleaning up stitched MP4 files..."
  python3 -c "
import json, os
with open('$OUTPUT_DIR/.upload_results.json') as f:
    results = json.load(f)
for r in results:
    if r.get('uploaded'):
        mp4 = '$OUTPUT_DIR/' + r['timestamp'] + '.mp4'
        if os.path.exists(mp4):
            os.remove(mp4)
            print(f'  Deleted: {mp4}')
"
fi

# ── 6. Summary ──────────────────────────────────────────────────────────────

if [ -f "$OUTPUT_DIR/.upload_results.json" ]; then
  UPLOAD_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_DIR/.upload_results.json'))))")
  log "Pipeline complete — $UPLOAD_COUNT video(s) processed"

  # Show a macOS notification if not running in a terminal
  if ! [ -t 0 ]; then
    osascript -e "display notification \"$UPLOAD_COUNT video(s) uploaded to YouTube\" with title \"HelmLog Video Pipeline\" sound name \"Glass\"" 2>/dev/null || true
  fi
else
  log "Pipeline complete — no videos uploaded"
fi
