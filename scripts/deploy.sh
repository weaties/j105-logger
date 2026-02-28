#!/usr/bin/env bash
# deploy.sh — Pull latest code from main, provision Grafana, and restart services.
#
# Run this on the Raspberry Pi after merging a PR to main. Handles the
# common case: code changes, dashboard/annotation updates, plugin installs.
# provision-grafana.sh is called every time and is fully idempotent.
#
# If systemd service files changed, also run:
#   ./scripts/setup.sh && sudo systemctl daemon-reload

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "==> Pulling latest from main..."
git checkout main
git pull origin main

echo "==> Syncing Python dependencies..."
uv sync

echo "==> Provisioning Grafana (dashboard, datasources, plugins)..."
"$SCRIPT_DIR/provision-grafana.sh"

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
sudo systemctl status j105-logger --no-pager -l
