#!/usr/bin/env bash
# deploy.sh — Pull latest code from main, provision Grafana, and restart services.
#
# Run this on the Raspberry Pi after merging a PR to main. Handles the
# common case: code changes, dashboard/annotation updates, plugin installs.
# provision-grafana.sh is called every time and is fully idempotent.
# Tailscale Funnel routes are re-applied on every deploy (idempotent).
#
# If systemd service files or apt packages changed, also run:
#   ./scripts/setup.sh && sudo systemctl daemon-reload

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

cd "$PROJECT_DIR"

echo "==> Pulling latest from main..."
git checkout main
git pull origin main

echo "==> Syncing Python dependencies..."
uv sync

echo "==> Provisioning Grafana (dashboard, datasources, plugins)..."
"$SCRIPT_DIR/provision-grafana.sh"

# ---------------------------------------------------------------------------
# Tailscale Funnel routes — re-applied on every deploy (idempotent, fast)
# Also updates PUBLIC_URL in .env so the app generates correct deep-links.
# ---------------------------------------------------------------------------
echo "==> Configuring Tailscale Funnel routes..."
if command -v tailscale &>/dev/null; then
    TS_HOSTNAME="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || echo '')"
    if [[ -n "$TS_HOSTNAME" ]]; then
        sudo tailscale set --operator="$(id -un)"
        tailscale funnel --bg 3002
        tailscale funnel --bg --set-path /grafana/ 3001
        tailscale funnel --bg --set-path /signalk/ 3000
        echo "    Routes verified for https://${TS_HOSTNAME}"
        # Keep PUBLIC_URL in .env current so the webapp generates correct links
        PUBLIC_URL_VALUE="https://${TS_HOSTNAME}"
        if [[ -f "$ENV_FILE" ]]; then
            if grep -q '^PUBLIC_URL=' "$ENV_FILE" 2>/dev/null; then
                sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=${PUBLIC_URL_VALUE}|" "$ENV_FILE"
            else
                printf '\nPUBLIC_URL=%s\n' "${PUBLIC_URL_VALUE}" >> "$ENV_FILE"
            fi
        fi
    else
        echo "    Tailscale not connected — skipping (run 'tailscale up' then re-deploy)."
    fi
else
    echo "    tailscale CLI not found — skipping."
fi

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
sudo systemctl status j105-logger --no-pager -l
