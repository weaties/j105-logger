#!/usr/bin/env bash
# deploy.sh — Pull latest code from main and restart the logger service.
#
# Run this on the Raspberry Pi after merging a PR to main. Covers the
# common case: code-only changes with no new system dependencies or
# service file modifications.
#
# If pyproject.toml changed (new deps) or systemd service files changed,
# run the full idempotent setup instead:
#   ./scripts/setup.sh && sudo systemctl daemon-reload
#
# Usage: ./scripts/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "==> Pulling latest from main..."
git checkout main
git pull origin main

echo "==> Syncing Python dependencies..."
uv sync

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
sudo systemctl status j105-logger --no-pager -l
