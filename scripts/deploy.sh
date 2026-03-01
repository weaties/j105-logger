#!/usr/bin/env bash
# deploy.sh — Pull latest code from main, provision Grafana, and restart services.
#
# Run this on the Raspberry Pi after merging a PR to main. Handles the
# common case: code changes, dashboard/annotation updates, plugin installs.
# provision-grafana.sh is called every time and is fully idempotent.
# Tailscale Funnel routes are re-applied on every deploy (idempotent).
#
# All sudo commands used here are in /etc/sudoers.d/j105-logger-allowed
# so they run without a password prompt (set up by setup.sh).
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
# Also updates PUBLIC_URL in .env and Grafana ROOT_URL so deep-links stay current.
# ---------------------------------------------------------------------------
echo "==> Configuring Tailscale Funnel routes..."
if command -v tailscale &>/dev/null; then
    TS_HOSTNAME="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\\.$//' || echo '')"
    if [[ -n "$TS_HOSTNAME" ]]; then
        tailscale funnel --bg 3002
        tailscale funnel --bg --set-path /grafana/ 3001
        tailscale funnel --bg --set-path /signalk/ 3000
        echo "    Routes verified for https://${TS_HOSTNAME}"
        # Update Grafana ROOT_URL with the actual public hostname
        sudo tee /etc/systemd/system/grafana-server.service.d/port.conf > /dev/null << EOF
[Service]
Environment=GF_SERVER_HTTP_PORT=3001
Environment=GF_SERVER_ROOT_URL=https://${TS_HOSTNAME}/grafana/
Environment=GF_SERVER_HTTP_ADDR=127.0.0.1
Environment=GF_AUTH_DISABLE_LOGIN_FORM=false
Environment=GF_AUTH_ANONYMOUS_ENABLED=false
EOF
        sudo systemctl daemon-reload
        sudo systemctl restart grafana-server
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
