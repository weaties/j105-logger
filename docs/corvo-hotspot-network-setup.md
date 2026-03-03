# Corvo Hotspot & Network Setup

Raspberry Pi as a WiFi hotspot backed by iPhone USB tethering, with split-horizon
DNS so `corvo.live.saillog.io` resolves locally on the hotspot and publicly
everywhere else.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Raspberry Pi (corvo)                                   │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐             │
│  │ hostapd  │  │ dnsmasq  │  │ j105-     │             │
│  │ (wlan0)  │  │ DHCP+DNS │  │ logger    │             │
│  └──────────┘  └──────────┘  │ :3002     │             │
│        │              │      └───────────┘             │
│        └──────────────┘            │                    │
│                │                   │                    │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐             │
│  │  eth1    │  │ tailscale│  │ Signal K  │             │
│  │ (iPhone) │  │ (tailnet)│  │ :3000     │             │
│  └──────────┘  └──────────┘  └───────────┘             │
└─────────────────────────────────────────────────────────┘

iPhone USB tethering → eth1 (ipheth driver, auto-detected)
Hotspot clients connect to wlan0 → 192.168.4.0/24
NAT forwards hotspot traffic through eth1 to the internet
Tailscale stays up through any internet path (iPhone or WiFi)
```

---

## Step 1 — iPhone USB Tethering

The iPhone connects via a Lightning-to-USB-A cable (with USB-C adapter for Pi 5).
The `ipheth` kernel driver creates an ethernet interface (typically `eth1`).

### Prerequisites

```bash
sudo apt install usbmuxd libimobiledevice-utils
```

These handle the USB multiplexing protocol that iPhones use. `usbmuxd` starts
automatically when an iPhone is plugged in.

### Usage

1. Plug the iPhone into a USB 3.0 port on the Pi
2. On the iPhone: tap **Trust** when prompted
3. Enable **Personal Hotspot** on the iPhone (Settings → Personal Hotspot)
4. The Pi should get an IP automatically via DHCP:

```bash
ip addr show eth1          # Should have a 172.20.10.x address
ping -I eth1 8.8.8.8       # Verify internet works
```

> The iPhone always assigns addresses in the `172.20.10.0/28` range for USB
> tethering. The interface name is `eth1` because `eth0` is the built-in
> ethernet port (which may or may not be connected).

### Pairing (first time only)

If the iPhone doesn't show up, pair it manually:

```bash
idevicepair pair           # Accept the trust prompt on the iPhone
idevice_id -l              # Should show the device UUID
```

---

## Step 2 — Static IP on wlan0

NetworkManager manages the Pi's network by default. Tell it to leave `wlan0`
alone so hostapd can manage it instead.

Create `/etc/NetworkManager/conf.d/unmanaged-wlan0.conf`:

```ini
[keyfile]
unmanaged-devices=interface-name:wlan0
```

Set a static IP via systemd-networkd. Create `/etc/systemd/network/10-wlan0-hotspot.network`:

```ini
[Match]
Name=wlan0

[Network]
Address=192.168.4.1/24
```

```bash
sudo systemctl enable systemd-networkd
sudo systemctl restart NetworkManager
sudo systemctl restart systemd-networkd
```

---

## Step 3 — hostapd (WiFi Access Point)

```bash
sudo apt install hostapd
sudo systemctl unmask hostapd
sudo systemctl enable hostapd
```

Create `/etc/hostapd/hostapd.conf`:

```ini
interface=wlan0
driver=nl80211
ssid=Corvo
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=YOUR_WIFI_PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
```

> 2.4 GHz (`hw_mode=g`) has better range — matters on a boat. Change `ssid` and
> `wpa_passphrase`.

```bash
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' | sudo tee /etc/default/hostapd
```

---

## Step 4 — dnsmasq (DHCP + Split-Horizon DNS)

Hotspot clients get the Pi as their DNS server via DHCP. dnsmasq resolves
`corvo.live.saillog.io` to the Pi's local IP, so web app traffic stays on the
LAN. No client modification needed.

```bash
sudo apt install dnsmasq
```

Create `/etc/dnsmasq.d/hotspot.conf`:

```ini
# Only listen on the hotspot interface
interface=wlan0
bind-interfaces

# DHCP range for hotspot clients
dhcp-range=192.168.4.10,192.168.4.150,255.255.255.0,24h

# ============================================
# SPLIT-HORIZON DNS
# Hotspot clients resolve this to the Pi.
# Everyone else hits Cloudflare normally.
# ============================================
address=/corvo.live.saillog.io/192.168.4.1

# Upstream DNS for everything else
server=8.8.8.8
server=8.8.4.4
```

Disable systemd-resolved if it's holding port 53:

```bash
sudo systemctl disable systemd-resolved
sudo systemctl stop systemd-resolved
sudo rm /etc/resolv.conf
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
```

---

## Step 5 — NAT & IP Forwarding

NAT masquerades hotspot traffic through the iPhone tethering interface (`eth1`).

```bash
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/90-hotspot.conf
sudo sysctl -p /etc/sysctl.d/90-hotspot.conf

sudo apt install iptables-persistent
sudo iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o eth1 -j ACCEPT
sudo iptables -A FORWARD -i eth1 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
```

---

## Step 6 — TLS Certificate (Let's Encrypt + Cloudflare DNS-01)

> **Optional** — only needed if you want `https://corvo.live.saillog.io` to work
> with a real TLS certificate. For hotspot-only use, the j105-logger web UI at
> `http://192.168.4.1:3002` works without TLS.

DNS-01 challenges work even when the Pi isn't publicly reachable on port 80.
Certbot creates a TXT record via the Cloudflare API to prove ownership.

```bash
sudo apt install certbot python3-certbot-dns-cloudflare
```

Create `/etc/letsencrypt/cloudflare.ini`:

```ini
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
```

```bash
sudo chmod 600 /etc/letsencrypt/cloudflare.ini
```

Create the token at https://dash.cloudflare.com/profile/api-tokens with
**Zone → DNS → Edit** scoped to `saillog.io`.

```bash
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d corvo.live.saillog.io \
  --preferred-challenges dns-01
```

Verify auto-renewal: `sudo systemctl status certbot.timer`

---

## Step 7 — nginx Reverse Proxy

> **Optional** — only needed if using TLS (Step 6).

```bash
sudo apt install nginx
```

Create `/etc/nginx/sites-available/corvo`:

```nginx
server {
    listen 80;
    server_name corvo.live.saillog.io;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name corvo.live.saillog.io;

    ssl_certificate     /etc/letsencrypt/live/corvo.live.saillog.io/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/corvo.live.saillog.io/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/corvo /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
```

Auto-reload after cert renewal — create
`/etc/letsencrypt/renewal-hooks/post/reload-nginx.sh`:

```bash
#!/bin/bash
systemctl reload nginx
```

```bash
sudo chmod +x /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh
```

---

## Step 8 — Cloudflare DNS (Public Side)

> **Optional** — only needed for public access via `corvo.live.saillog.io`.

In the Cloudflare dashboard for `saillog.io`, add an **A record**:
`corvo.live` → iPhone's public IP. Use **DNS only** (grey cloud) for direct
connections or **Proxied** (orange cloud) for DDoS protection.

### Dynamic DNS Script

iPhone cellular IPs change frequently. Create `/usr/local/bin/cloudflare-ddns.sh`:

```bash
#!/bin/bash
CF_API_TOKEN="YOUR_CLOUDFLARE_API_TOKEN"
ZONE_ID="YOUR_ZONE_ID"
RECORD_NAME="corvo.live.saillog.io"
UPLINK_IF="eth1"

CURRENT_IP=$(curl -s --interface "$UPLINK_IF" https://api.ipify.org)
[ -z "$CURRENT_IP" ] && echo "$(date): No IP" >&2 && exit 1

CACHE_FILE="/tmp/ddns-last-ip"
LAST_IP=$(cat "$CACHE_FILE" 2>/dev/null)
[ "$CURRENT_IP" = "$LAST_IP" ] && exit 0

RECORD_ID=$(curl -s -X GET \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?name=${RECORD_NAME}" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" | jq -r '.result[0].id')

[ -z "$RECORD_ID" ] || [ "$RECORD_ID" = "null" ] && echo "$(date): No record ID" >&2 && exit 1

RESULT=$(curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "{\"type\":\"A\",\"name\":\"${RECORD_NAME}\",\"content\":\"${CURRENT_IP}\",\"ttl\":300}")

if [ "$(echo "$RESULT" | jq -r '.success')" = "true" ]; then
    echo "$CURRENT_IP" > "$CACHE_FILE"
    echo "$(date): Updated to ${CURRENT_IP}"
else
    echo "$(date): Failed: $RESULT" >&2 && exit 1
fi
```

```bash
sudo chmod +x /usr/local/bin/cloudflare-ddns.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/cloudflare-ddns.sh >> /var/log/ddns.log 2>&1") | crontab -
```

---

## Step 9 — Tailscale Considerations

Tailscale uses its own `tailscale0` interface and routing table — it doesn't
interfere with the hotspot. SSH access via the Tailscale IP works regardless of
hotspot or iPhone state.

The web app is reachable at both:
- `http://192.168.4.1:3002` (from hotspot clients)
- `https://corvopi.taileb1513.ts.net/` (via Tailscale Funnel)

If Tailscale DNS overrides local resolution:

```bash
sudo tailscale set --accept-dns=false
```

---

## Automation

Steps 1–5 are automated in `scripts/setup.sh` (section a.2). The script:
- Installs `usbmuxd`, `libimobiledevice-utils`, `hostapd`, `dnsmasq`, `iptables-persistent`
- Configures NetworkManager to leave wlan0 unmanaged
- Sets the static IP via systemd-networkd
- Writes the dnsmasq hotspot config
- Enables IP forwarding and NAT rules
- Enables all services for boot

The only manual step is creating `/etc/hostapd/hostapd.conf` with your chosen
SSID and password (setup.sh warns if it's missing).

---

## Verification Checklist

```bash
# iPhone tethering
ip addr show eth1                                    # 172.20.10.x address
ping -I eth1 8.8.8.8                                 # internet works

# Hotspot
sudo systemctl status hostapd                        # AP running
sudo systemctl status dnsmasq                        # DHCP+DNS running
iw dev wlan0 info                                    # type AP, ssid Corvo

# From a device connected to the Corvo hotspot:
# ping 192.168.4.1                                   # Pi reachable
# open http://192.168.4.1:3002                       # j105-logger web UI

# DNS (optional, if using corvo.live.saillog.io)
dig @192.168.4.1 corvo.live.saillog.io               # → 192.168.4.1

# NAT
sudo iptables -t nat -L -v                           # MASQUERADE on eth1
sysctl net.ipv4.ip_forward                           # = 1

# Tailscale (always works independently)
tailscale status                                     # tailscale up
```

---

## Troubleshooting

| Problem | Check |
|---|---|
| iPhone not detected | `lsusb` for Apple device; `dmesg \| grep ipheth`; is usbmuxd running? |
| No internet for hotspot clients | `iptables -t nat -L` for MASQUERADE on eth1; `sysctl net.ipv4.ip_forward` = 1 |
| eth1 has no IP | iPhone Personal Hotspot enabled? Tap **Trust** on phone? Try `sudo usbmuxd -f -v` |
| hostapd fails to start | `journalctl -u hostapd` — check for driver/channel conflict; is NM still managing wlan0? |
| dnsmasq won't start | `ss -tlnp \| grep :53` — port 53 conflict (systemd-resolved?) |
| Domain doesn't resolve locally | `dig @192.168.4.1 corvo.live.saillog.io` should return `192.168.4.1` |
| wlan0 has wrong IP | Check `/etc/systemd/network/10-wlan0-hotspot.network`; restart systemd-networkd |
| DDNS not updating | `/var/log/ddns.log`; verify Cloudflare token + zone ID |

---

## File Reference

| File | Purpose |
|---|---|
| `/etc/hostapd/hostapd.conf` | WiFi AP config (SSID, password, channel) |
| `/etc/default/hostapd` | Points to hostapd.conf |
| `/etc/dnsmasq.d/hotspot.conf` | DHCP + split-horizon DNS |
| `/etc/sysctl.d/90-hotspot.conf` | IP forwarding |
| `/etc/systemd/network/10-wlan0-hotspot.network` | Static IP for wlan0 |
| `/etc/NetworkManager/conf.d/unmanaged-wlan0.conf` | Keeps NM off wlan0 |
| `/etc/iptables/rules.v4` | Persisted NAT/forwarding rules |
| `/etc/nginx/sites-available/corvo` | TLS + reverse proxy (optional) |
| `/etc/letsencrypt/cloudflare.ini` | Cloudflare API creds for certbot (optional) |
| `/usr/local/bin/cloudflare-ddns.sh` | Dynamic DNS updater (optional) |
