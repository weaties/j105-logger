#!/usr/bin/env bash
# setup.sh — One-shot setup for j105-logger on Raspberry Pi.
#
# Usage:
#   ./scripts/setup.sh
#
# Safe to re-run after `git pull` — all steps are idempotent.
# Requires sudo for systemd service installation and group membership changes.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(id -un)"
ENV_FILE="$PROJECT_DIR/.env"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step()  { echo -e "\n${GREEN}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
error() { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------

step "Checking uv..."

UV_BIN=""
if command -v uv &>/dev/null; then
    UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi

if [[ -z "$UV_BIN" ]]; then
    step "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV_BIN="$HOME/.local/bin/uv"
fi

echo "  uv: $UV_BIN ($("$UV_BIN" --version))"

# ---------------------------------------------------------------------------
# 2. Python dependencies
# ---------------------------------------------------------------------------

step "Syncing Python dependencies..."
"$UV_BIN" sync --project "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------

step "Configuring environment..."
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    echo "  Created .env from template."
    echo "  Edit $ENV_FILE if your CAN interface name or bitrate differs."
else
    echo "  .env already exists — leaving untouched."
fi

# Load CAN_INTERFACE and CAN_BITRATE from .env for use in service generation.
# Strip comments and blank lines; export only KEY=VALUE pairs.
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | grep -v '^#')
set +a

CAN_INTERFACE="${CAN_INTERFACE:-can0}"
CAN_BITRATE="${CAN_BITRATE:-250000}"

# ---------------------------------------------------------------------------
# 4. data directory
# ---------------------------------------------------------------------------

step "Ensuring data directory exists..."
mkdir -p "$PROJECT_DIR/data"
echo "  $PROJECT_DIR/data"

# ---------------------------------------------------------------------------
# 5. netdev group (allows non-root SocketCAN access)
# ---------------------------------------------------------------------------

step "Checking group membership..."
if ! id -nG "$CURRENT_USER" | grep -qw netdev; then
    sudo usermod -aG netdev "$CURRENT_USER"
    warn "Added $CURRENT_USER to the 'netdev' group."
    warn "A reboot (or 'newgrp netdev') is required for this to take effect."
else
    echo "  $CURRENT_USER is already in the 'netdev' group."
fi

# ---------------------------------------------------------------------------
# 6. CAN interface systemd service
# ---------------------------------------------------------------------------

step "Installing CAN interface service (interface=$CAN_INTERFACE bitrate=$CAN_BITRATE)..."

sudo tee /etc/systemd/system/can-interface.service > /dev/null <<EOF
[Unit]
Description=SocketCAN interface ${CAN_INTERFACE}
# Wait for the kernel network device to appear (requires HAT + SPI overlay).
After=sys-subsystem-net-devices-${CAN_INTERFACE}.device
BindsTo=sys-subsystem-net-devices-${CAN_INTERFACE}.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/sbin/ip link set ${CAN_INTERFACE} down 2>/dev/null || true
ExecStart=/sbin/ip link set ${CAN_INTERFACE} up type can bitrate ${CAN_BITRATE}
ExecStop=/sbin/ip link set ${CAN_INTERFACE} down

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable can-interface.service

# Start only if the interface device is already present (HAT is configured).
if ip link show "$CAN_INTERFACE" &>/dev/null; then
    sudo systemctl restart can-interface.service
    echo "  CAN interface ${CAN_INTERFACE} is up."
else
    warn "Network device '${CAN_INTERFACE}' not found — service enabled but not started."
    warn "If your CAN HAT isn't configured yet:"
    warn "  1. Add the correct dtoverlay to /boot/firmware/config.txt (or /boot/config.txt)"
    warn "     e.g.: dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25"
    warn "  2. Reboot the Pi."
    warn "  3. Re-run this script."
fi

# ---------------------------------------------------------------------------
# 7. j105-logger systemd service
# ---------------------------------------------------------------------------

step "Installing j105-logger service..."

sudo tee /etc/systemd/system/j105-logger.service > /dev/null <<EOF
[Unit]
Description=J105 NMEA 2000 Data Logger
After=can-interface.service
Requires=can-interface.service

[Service]
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${UV_BIN} run --project ${PROJECT_DIR} j105-logger
Restart=on-failure
RestartSec=5
# Allow read access to the CAN socket
SupplementaryGroups=netdev

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable j105-logger.service

if sudo systemctl is-active --quiet can-interface.service 2>/dev/null; then
    sudo systemctl restart j105-logger.service
    echo "  Logger service started."
else
    echo "  Logger service installed and enabled (will start once CAN interface is up)."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "  View logs:    sudo journalctl -fu j105-logger"
echo "  Status:       sudo systemctl status j105-logger"
echo "  Stop:         sudo systemctl stop j105-logger"
echo "  Start:        sudo systemctl start j105-logger"
echo ""
echo "  To update after a git pull:"
echo "    git pull && ./scripts/setup.sh"
