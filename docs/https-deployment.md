# HTTPS Deployment Guide

The HelmLog web interface (`WEB_PORT=3002` by default) must be served over
HTTPS before exposing it to the public internet.  The application itself does
not terminate TLS — choose one of the three approaches below.

---

## Option A — Cloudflare Tunnel (recommended — automated by setup.sh)

Cloudflare Tunnel gives each boat a public `<boat>.helmlog.org` subdomain with
zero port-forwarding.  Works behind CGNAT and requires no inbound firewall
rules.  Each Pi runs its own `cloudflared` tunnel — no central proxy or shared
state.

### How it works

| URL | Routes to |
|---|---|
| `boat.helmlog.org/` | helmlog web UI |
| `boat.helmlog.org/grafana/` | Grafana dashboards |
| `boat.helmlog.org/signalk/` | Signal K explorer |
| `boat.helmlog.org/sk/` | Signal K admin UI |

The existing nginx reverse proxy on port 80 handles all path routing — the
tunnel just points at `localhost:80`.

### Automated setup (recommended)

1. Create a tunnel in the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/)
   - Zone: `helmlog.org` (or your own domain)
   - Public hostname: `boat.helmlog.org`
   - Service: `http://localhost:80`
2. Copy the connector token from the dashboard
3. Run the configuration wizard:

```bash
./scripts/configure.sh
# Enter the tunnel token and hostname when prompted
```

4. Run setup.sh (or re-run it if already set up):

```bash
./scripts/setup.sh
```

That's it. `setup.sh` installs `cloudflared`, registers the systemd service,
and starts the tunnel.  The public URL is shown in the setup summary.

### Unattended setup (CI / scripted installs)

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi... \
CLOUDFLARE_HOSTNAME=boat.helmlog.org \
ADMIN_EMAIL=you@example.com \
  ./scripts/configure.sh --non-interactive

./scripts/setup.sh
```

Or via bootstrap.sh:

```bash
curl -fsSL .../bootstrap.sh \
  | ADMIN_EMAIL=you@example.com \
    CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi... \
    CLOUDFLARE_HOSTNAME=boat.helmlog.org \
    bash
```

### Managing the tunnel

```bash
# Check status
sudo systemctl status cloudflared

# View logs
sudo journalctl -fu cloudflared

# Restart after config changes
sudo systemctl restart cloudflared

# Update token (re-run configure.sh, then setup.sh)
./scripts/configure.sh
./scripts/setup.sh
```

### Multi-boat scalability

Each boat runs its own tunnel — one boat going offline doesn't affect others.
DNS is a CNAME per boat (`boat.helmlog.org → <tunnel-id>.cfargotunnel.com`).
Create the tunnel in the Cloudflare dashboard, run `configure.sh` on the Pi,
and you're done.

### Auth safety

**Authentication must be enabled** when using Cloudflare Tunnel.  `setup.sh`
warns if `AUTH_DISABLED=true` is set alongside a tunnel token.  Consider also
enabling [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
(free tier) for an additional auth layer.

### Manual setup (alternative)

If you prefer not to use the configuration wizard:

```bash
# Install cloudflared
curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture).deb" \
  -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

# Install as service with connector token from dashboard
sudo cloudflared service install <TOKEN>
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

---

## Option B — Caddy Reverse Proxy

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

## Option C — Tailscale Funnel (handled automatically by setup.sh)

Tailscale Funnel exposes the Pi to the public internet under a permanent
`ts.net` HTTPS URL — no separate domain or certificate needed.

**`setup.sh` and `deploy.sh` both configure this automatically** when Tailscale
is already authenticated on the Pi. The three public routes are:

| Path | Local service | Purpose |
|---|---|---|
| `/` | port 3002 | helmlog race marker |
| `/grafana/` | port 3001 | Grafana dashboards |
| `/signalk/` | port 3000 | Signal K explorer |

Run `tailscale funnel status` to confirm, or let `setup.sh` report it.

**Prerequisite (one-time per Pi):**

```bash
sudo tailscale set --operator=$USER
```

This is also done automatically by `setup.sh`, but if running the commands
manually for the first time, do this step first.

**To manually configure:**

```bash
sudo tailscale set --operator=$USER
tailscale funnel --bg 3002
tailscale funnel --bg --set-path /grafana/ 3001
tailscale funnel --bg --set-path /signalk/ 3000
```

Your logger is then accessible at `https://<pi-hostname>.<tailnet>.ts.net`. Run
`tailscale funnel status` to see the exact public URL.

> **Note**: Tailscale Funnel strips the path prefix when proxying — Grafana
> receives `/d/...` not `/grafana/d/...`. This is handled automatically by
> `setup.sh`/`deploy.sh` which write the correct `GF_SERVER_ROOT_URL` to
> Grafana's `port.conf`. **Do not** set `GF_SERVER_SERVE_FROM_SUB_PATH=true`
> — it causes redirect loops with this Funnel setup.

---

## Setting up the first admin user

After deploying, create the first admin account with the CLI:

```bash
helmlog create-admin --email you@example.com
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

---

## Operator configuration persistence

All operator-supplied settings (email, SMTP, Cloudflare token) are stored in
`~/.helmlog/config.env`, which survives `reset-pi.sh`.  Run
`./scripts/configure.sh` at any time to update settings, or
`./scripts/configure.sh --show` to view current values.

| File | Survives reset? | Purpose |
|---|---|---|
| `~/.helmlog/config.env` | Yes | Operator identity + external service credentials |
| `.env` | No | Runtime config (generated by setup.sh from .env.example + config.env) |
