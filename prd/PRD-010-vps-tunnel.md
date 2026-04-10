# PRD-010: Move Cloudflare Tunnel to VPS

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-11

---

## Problem

The Cloudflare tunnel runs on the iMac, which sits behind a home internet connection. When the ISP has a brief dropout (even 1–2 seconds), all 4 tunnel connections drop simultaneously and the site shows a Cloudflare error for ~5–10 seconds. This happens several times a day. No amount of cloudflared tuning fixes it — it's a network stability issue.

---

## Goal

Move cloudflared to a cheap VPS with a stable datacenter connection. The VPS connects to Cloudflare's edge (stable), and proxies requests back to the iMac over Tailscale (wobbles don't matter — only affects latency slightly, not availability).

```
User → Cloudflare edge → VPS (cloudflared) → Tailscale → iMac (server.py)
```

Instead of:

```
User → Cloudflare edge → iMac (cloudflared + server.py)  ← breaks when ISP hiccups
```

---

## Recommended VPS

**Hetzner CAX11** (ARM, Helsinki or Falkenstein)
- €3.29/month, 2 vCPU, 4GB RAM, 40GB SSD
- Excellent network, very stable
- Accepts credit card, no nonsense signup

Alternative: **DigitalOcean Basic** $4/month, or **Vultr** $2.50/month. Any will do — we're running ~5MB/s peak traffic at most.

---

## Setup Steps

### 1. Create VPS

Sign up at hetzner.com, create a CAX11 (or CX22) instance:
- OS: Ubuntu 24.04
- Region: Falkenstein or Helsinki (closer to Melbourne Cloudflare edge than US)
- Add your SSH public key at creation time

Note the public IP — call it `VPS_IP`.

### 2. Install Tailscale on the VPS

```bash
ssh root@VPS_IP
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

Follow the auth link in the output. The VPS will appear in your Tailscale admin console — note its Tailscale IP (e.g. `100.x.y.z`). Call it `VPS_TAILSCALE_IP`.

### 3. Install cloudflared on the VPS

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
  gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflare focal main' \
  > /etc/apt/sources.list.d/cloudflare.list
apt update && apt install -y cloudflared
```

### 4. Copy tunnel credentials to VPS

The tunnel credentials live on the iMac at:
- `~/.cloudflared/c34ed65f-88f8-48b1-88ea-45792e51a5a6.json`

Copy to VPS:
```bash
# On iMac
scp ~/.cloudflared/c34ed65f-88f8-48b1-88ea-45792e51a5a6.json root@VPS_IP:/etc/cloudflared/
```

### 5. Create config on VPS

```bash
cat > /etc/cloudflared/config.yml << EOF
tunnel: c34ed65f-88f8-48b1-88ea-45792e51a5a6
credentials-file: /etc/cloudflared/c34ed65f-88f8-48b1-88ea-45792e51a5a6.json
protocol: http2

ingress:
  - hostname: search.hughwilliams.com
    service: http://IMAC_TAILSCALE_IP:8765
  - service: http_status:404
EOF
```

Replace `IMAC_TAILSCALE_IP` with the iMac's Tailscale IP (`100.112.89.24`).

### 6. Run cloudflared as a system service on VPS

```bash
cloudflared service install
systemctl enable cloudflared
systemctl start cloudflared
systemctl status cloudflared
```

Check logs:
```bash
journalctl -u cloudflared -f
```

Should show 4× `Registered tunnel connection ... protocol=http2`.

### 7. Stop cloudflared on the iMac

Once VPS tunnel is confirmed working:

```bash
# On iMac
launchctl unload ~/Library/LaunchAgents/com.cloudflared-zettair.plist
```

Keep the plist file — just unloaded, not deleted — in case you need to fall back.

### 8. Update watchdog.sh on iMac

The watchdog currently checks the cloudflared log on the iMac. Update it to instead check that the VPS is reachable over Tailscale and that the tunnel is alive:

```bash
# Replace the cloudflare section in watchdog.sh with:
if ssh -o ConnectTimeout=5 -o BatchMode=yes root@VPS_TAILSCALE_IP \
    "systemctl is-active --quiet cloudflared"; then
    log "OK cloudflared (VPS)"
else
    log "FAIL cloudflared (VPS) — restarting"
    ssh root@VPS_TAILSCALE_IP "systemctl restart cloudflared"
fi
```

Add the VPS to `~/.ssh/known_hosts` first: `ssh root@VPS_TAILSCALE_IP` once manually.

---

## What Changes

| Component | Before | After |
|-----------|--------|-------|
| cloudflared location | iMac (home ISP) | VPS (datacenter) |
| Tunnel endpoint | Cloudflare → iMac directly | Cloudflare → VPS → Tailscale → iMac |
| Failure mode | ISP dropout = 502 | ISP dropout = slightly higher latency, no 502 |
| iMac firewall | Port 8765 exposed to Tailscale only (unchanged) | Same |
| Latency added | 0ms | ~5–10ms (VPS→iMac Tailscale hop) |
| Monthly cost | $0 | ~€3.29/month |

---

## Rollback

If anything goes wrong:
1. `launchctl load ~/Library/LaunchAgents/com.cloudflared-zettair.plist` on iMac — tunnel is back on iMac within seconds
2. Update VPS config `service:` line to point to `http://localhost:8765` if you ever move server.py to the VPS instead

---

## Notes

- The iMac's `server.py` doesn't need to change — it still listens on `0.0.0.0:8765`, Tailscale handles the access control
- Tailscale ACLs should already allow VPS → iMac on port 8765; if not, add a rule in the Tailscale admin console
- The VPS doesn't need much — it's purely a tunnel relay. 512MB RAM would be sufficient
- Hetzner bills by the hour, so you can try it and delete the VPS with no commitment
