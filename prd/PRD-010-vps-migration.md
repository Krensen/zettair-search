# PRD-010: Migrate to VPS — Full Stack, CI/CD, Custom Domain

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-13

---

## Problem

The search engine runs on a home iMac behind a residential ISP. This causes:
- Frequent brief outages when the home internet drops (all 4 Cloudflare tunnel connections die simultaneously)
- No separation between development machine and production
- No automated deployment — changes require manual server restarts
- Scaling to full English Wikipedia (~23GB dump, ~6.7M articles) is impractical on a machine that's also a daily driver

---

## Goal

Move everything to a VPS. The iMac becomes a development machine only. Code is pushed to GitHub, CI/CD deploys to the VPS automatically. The search engine gets a dedicated domain name.

```
Dev (anywhere)
    │ git push
    ▼
GitHub
    │ GitHub Actions (CI/CD)
    ▼
VPS (Hetzner)
    │ cloudflared
    ▼
Cloudflare edge → search.yourdomain.com
```

---

## Architecture

### VPS

**Hetzner CAX21** (recommended)
- 4GB RAM, 2 ARM vCPUs, 40GB SSD — €5.49/month
- Datacenter: Falkenstein or Helsinki
- OS: Ubuntu 24.04 ARM64
- Why ARM: our Zettair binary already builds on ARM (Apple Silicon fixes carry over); Hetzner ARM is cheapest per GB RAM

**Why 4GB not 2GB:**
Current Simple English Wikipedia working set is ~550MB. 2GB would work today, but full English Wikipedia (6.7M articles) will need:
- Index: ~5–6GB on disk, memory-mapped by workers
- Snippets JSON: ~2–3GB in RAM (or switch to a different sidecar format)
- Docstore: ~15GB on disk (file seeks, not RAM)

4GB handles Simple English comfortably. Full English will need either an 8–16GB VPS or a redesign of the sidecar loading (lazy/mmap). That's a later problem — the code won't change, just the box size.

**Disk:** 40GB is enough for Simple English. Full English needs ~80–100GB total (dump + index + docstore). Hetzner allows online disk resize, so start small and grow when needed.

### Domain

Buy a dedicated domain (suggested: `zet.search`, `wikisearch.dev`, `searchzet.com`, or similar — your call). Point it at Cloudflare for DNS. Create a named tunnel `zettair` (or reuse existing tunnel UUID, just update the ingress hostname).

### Repository layout

No structural changes to `zettair-search` or `zettair` repos. Add:
- `deploy/` directory in `zettair-search`:
  - `setup.sh` — one-time VPS provisioning (install deps, compile zettair, build index)
  - `deploy.sh` — pull latest code from GitHub, restart server (called by CI/CD)
  - `systemd/` — systemd unit files for server.py and cloudflared (replacing launchd)

---

## CI/CD Pipeline

### Trigger
Push to `main` branch of `zettair-search`.

### What it does
1. **Test** — run a quick smoke test (import server.py, check syntax)
2. **Deploy** — SSH to VPS, run `deploy.sh`

### `deploy.sh` (runs on VPS)
```bash
cd /opt/zettair-search
git pull origin main
pip3 install -r requirements.txt --quiet
systemctl restart zettair-search
```

That's it. No Docker, no Kubernetes, no build artifacts. The VPS runs the code directly from the git checkout. Simple, debuggable, fast.

### GitHub Actions workflow (`.github/workflows/deploy.yml`)
```yaml
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        run: |
          echo "${{ secrets.VPS_SSH_KEY }}" > /tmp/key
          chmod 600 /tmp/key
          ssh -i /tmp/key -o StrictHostKeyChecking=no deploy@VPS_IP \
            "cd /opt/zettair-search && git pull && systemctl restart zettair-search"
```

**Secrets needed in GitHub:**
- `VPS_SSH_KEY` — private key of a `deploy` user on the VPS (no sudo, can only restart the service via sudoers rule)

### What CI/CD does NOT do
- Rebuild the Zettair index (takes ~30 min, only needed when Wikipedia dump updates)
- Rebuild the docstore (takes ~22 sec, only needed when dump updates)
- Recompile the C binary (only needed when `okapi.c` etc. change)

These are manual operations triggered separately. The deploy path is purely Python + HTML.

---

## Systemd Services (replacing launchd)

### `/etc/systemd/system/zettair-search.service`
```ini
[Unit]
Description=Zettair Search Service
After=network.target

[Service]
Type=simple
User=zettair
WorkingDirectory=/opt/zettair-search
Environment=ZET_BINARY=/opt/zettair/devel/zet
Environment=ZET_INDEX=/opt/zettair/wikiindex/index
Environment=ZET_CLICK_PRIOR=/opt/zettair/wikipedia/click_prior.bin
Environment=ZET_CLICK_ALPHA=0.5
Environment=ZET_WORKERS=2
Environment=ZET_SUMMARISE=1
Environment=ZET_DOCSTORE=/opt/zettair/wikipedia/simplewiki.docstore
Environment=ZET_DOCMAP=/opt/zettair/wikipedia/simplewiki.docmap
ExecStart=/usr/bin/python3 server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/cloudflared.service`
```ini
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=cloudflared
ExecStart=/usr/bin/cloudflared tunnel --protocol http2 run zettair-search
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Systemd's `Restart=always` is more reliable than launchd's `KeepAlive` — it handles crashes, OOM kills, and clean exits.

---

## Migration Steps

### Phase 1 — Provision VPS (manual, ~20 min)
1. Create Hetzner CAX21, Ubuntu 24.04, add SSH key
2. Create `zettair` user, `deploy` user
3. Install Python 3.11+, git, gcc, autoconf, make
4. Clone both repos: `git clone https://github.com/Krensen/zettair` and `zettair-search` into `/opt/`
5. Compile Zettair: `cd /opt/zettair/devel && ./configure --build=aarch64-unknown-linux-gnu && make`
6. Download Simple English Wikipedia dump and build index (run `wiki2trec.py`, `zet -i`, `build_docstore.py`, `build_click_prior.py`, `build_autosuggest.py`)
7. Install systemd services, enable and start them
8. Install cloudflared, copy tunnel credentials, start service

### Phase 2 — DNS cutover (manual, ~5 min)
1. In Cloudflare dashboard: update tunnel ingress to point at `http://localhost:8765` (since cloudflared now runs on same box as server)
2. Update hostname to new domain
3. Verify `https://yourdomain.com` works

### Phase 3 — CI/CD (manual setup, ~15 min)
1. Generate deploy SSH keypair: `ssh-keygen -t ed25519 -f deploy_key`
2. Add public key to `deploy` user's `~/.ssh/authorized_keys` on VPS
3. Add private key as `VPS_SSH_KEY` secret in GitHub repo settings
4. Add `VPS_IP` as secret
5. Commit `.github/workflows/deploy.yml`
6. Test: push a trivial change, watch Actions tab

### Phase 4 — Decommission iMac services (manual, ~5 min)
```bash
launchctl unload ~/Library/LaunchAgents/com.cloudflared-zettair.plist
launchctl unload ~/Library/LaunchAgents/com.zettair-search.plist
launchctl unload ~/Library/LaunchAgents/com.zettair-watchdog.plist
```
Keep the plists in case you want to run locally again.

---

## Full English Wikipedia (future)

When ready, the migration path is:
1. Resize VPS disk to 160GB (Hetzner online resize, no downtime)
2. Upgrade to CAX41 (16GB RAM) or add a second worker box
3. Download `enwiki-latest-pages-articles.xml.bz2` (~23GB compressed)
4. Run same pipeline: `wiki2trec.py` → `zet -i` → sidecars
5. Swap index path in systemd environment variables, restart

The code doesn't change. The sidecar loading may need to be rethought for the full corpus — `simplewiki_snippets.json` at 87MB is fine, but the full English equivalent would be ~2.5GB and slow to load. Options at that point: SQLite lookup, memory-mapped binary format, or just accept the 30-second startup time. Not a problem to solve now.

---

## Files to Add

| File | Purpose |
|------|---------|
| `deploy/setup.sh` | One-time VPS provisioning script |
| `deploy/deploy.sh` | Pull + restart (called by CI/CD and manually) |
| `deploy/zettair-search.service` | systemd unit for server.py |
| `deploy/cloudflared.service` | systemd unit for cloudflared |
| `.github/workflows/deploy.yml` | GitHub Actions CI/CD pipeline |
| `README.md` | Update with VPS setup instructions |

---

## Success Criteria

1. `git push` to main → site updated within 60 seconds, zero manual steps
2. Zero Cloudflare 502s from ISP dropouts (home internet no longer in the path)
3. Full pipeline documented so a fresh VPS can be set up from scratch following `setup.sh`
4. iMac can be turned off with no effect on the site
