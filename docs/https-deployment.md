# HTTPS Deployment Guide

The J105 Logger web interface (`WEB_PORT=3002` by default) must be served over
HTTPS before exposing it to the public internet.  The application itself does
not terminate TLS — choose one of the three approaches below.

---

## Option A — Caddy Reverse Proxy (simplest on Pi)

Caddy automatically obtains and renews a Let's Encrypt certificate.

### Install Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

### Configure (`/etc/caddy/Caddyfile`)

```
logger.yourboat.com {
    reverse_proxy localhost:3002
}
```

### Enable & start

```bash
sudo systemctl enable caddy
sudo systemctl start caddy
```

Caddy listens on ports 80 and 443.  Make sure both are open in your router /
firewall and that `logger.yourboat.com` has an A record pointing at the Pi's
public IP.

---

## Option B — Cloudflare Tunnel (zero port-forwarding)

Cloudflare Tunnel works behind CGNAT and requires no inbound firewall rules.

### Install cloudflared

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

### Create and configure the tunnel

```bash
# Authenticate (opens browser)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create j105-logger

# Create config at ~/.cloudflared/config.yml
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: j105-logger
credentials-file: /home/pi/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: logger.yourboat.com
    service: http://localhost:3002
  - service: http_status:404
EOF

# Add DNS record
cloudflared tunnel route dns j105-logger logger.yourboat.com

# Run as a service
sudo cloudflared service install
sudo systemctl start cloudflared
```

No router changes needed.  HTTPS is provided by Cloudflare.

---

## Option C — Tailscale Funnel (stay on Tailscale)

Tailscale Funnel exposes a Tailscale node to the public internet under a
`ts.net` HTTPS URL — no separate domain or certificate needed.

```bash
# Enable Funnel for port 3002
tailscale serve --bg http://localhost:3002
tailscale funnel --bg 443

# Your logger is now accessible at:
#   https://corvopi.<tailnet-name>.ts.net
tailscale funnel status
```

Sharing the `ts.net` URL with non-Tailscale users gives them public HTTPS access.
Tailscale handles certificate management automatically.

---

## Setting up the first admin user

After deploying, create the first admin account with the CLI:

```bash
j105-logger create-admin --email you@example.com
```

Or set `ADMIN_EMAIL=you@example.com` in `.env` — on startup the logger will
auto-create an admin user with that email and print a one-time invite link to
the log.

---

## Auth environment variables

| Variable | Default | Description |
|---|---|---|
| `AUTH_DISABLED` | `false` | Set to `true` to skip auth entirely (Tailscale-only installs) |
| `AUTH_SESSION_TTL_DAYS` | `90` | How long session cookies stay valid |
| `ADMIN_EMAIL` | — | Auto-create admin user on startup |

> **Tip:** If you're running the logger exclusively over Tailscale and don't
> need public access, set `AUTH_DISABLED=true` in `.env` to restore the
> original zero-friction behaviour.
