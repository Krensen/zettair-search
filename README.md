# Zettair Wikipedia Search

A full-text BM25 search engine over Simple English Wikipedia, running locally on a Mac with a permanent public URL via Cloudflare Tunnel.

**Live demo:** https://search.hughwilliams.com

---

## What it is

- **Search engine:** Zettair (BM25/Okapi), patched for Apple Silicon
- **Backend:** FastAPI Python server wrapping the `zet` CLI
- **Frontend:** Single-file Google-style HTML/JS UI with instant search, knowledge panel, and image thumbnails proxied from Wikimedia
- **Data:** Simple English Wikipedia (~256k articles, ~64k images)
- **Tunnel:** Cloudflare named tunnel → permanent public HTTPS URL

---

## Prerequisites

- macOS (Apple Silicon or Intel)
- Homebrew
- A Cloudflare account with your domain's nameservers pointed at Cloudflare
- ~5GB free disk space (XML dump + index)
- ~30 min for indexing

---

## Step 1 — Install dependencies

```bash
brew install python@3.11 cloudflared git
pip3 install fastapi uvicorn
```

---

## Step 2 — Clone the repos

```bash
mkdir -p ~/search && cd ~/search

# Search service (server + frontend)
git clone https://github.com/Krensen/zettair-search.git

# Zettair engine (patched source)
git clone https://github.com/Krensen/zettair.git
```

Your layout will be:
```
~/search/
  zettair-search/    ← FastAPI server + frontend
  zettair/           ← Zettair source + wiki tools
```

---

## Step 3 — Build Zettair

```bash
cd ~/search/zettair/devel

# Apple Silicon (M1/M2/M3/M4)
./configure --build=arm-apple-darwin

# Intel Mac
./configure

make
```

Binary will be at `~/search/zettair/devel/zet`. Test it:

```bash
echo "test" | ./zet --help
```

---

## Step 4 — Download Simple English Wikipedia

```bash
mkdir -p ~/search/zettair/wikipedia
cd ~/search/zettair/wikipedia

# Download the latest dump (~330MB compressed)
curl -O https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2

# Decompress (~1.5GB uncompressed — takes a few minutes)
bunzip2 simplewiki-latest-pages-articles.xml.bz2
mv simplewiki-latest-pages-articles.xml simplewiki.xml
```

---

## Step 5 — Convert to TREC format + extract snippets/images

```bash
cd ~/search/zettair/wikipedia
python3 wiki2trec.py
```

This reads `simplewiki.xml` and produces:
- `simplewiki.trec` — Zettair input format
- `simplewiki_snippets.json` — clean prose snippets per article
- `simplewiki_images.json` — Wikimedia CDN image URLs per article

Takes ~5–10 minutes. You'll see progress every 5000 articles.

---

## Step 6 — Index with Zettair

```bash
mkdir -p ~/search/zettair/wikiindex
cd ~/search/zettair/wikiindex

../devel/zet -i -f index ../wikipedia/simplewiki.trec
```

Takes ~5 minutes. Produces `index.*` files in the `wikiindex/` directory.

---

## Step 7 — Run the search server

```bash
cd ~/search/zettair-search

ZET_BINARY=~/search/zettair/devel/zet \
ZET_INDEX=~/search/zettair/wikiindex/index \
python3 server.py
```

Test locally: http://localhost:8765

The server loads both sidecar JSON files into memory on startup (~5 seconds).

---

## Step 8 — Set up Cloudflare Tunnel (permanent public URL)

### 8a. Log in to Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser. Authorise your domain. A cert is saved to `~/.cloudflared/cert.pem`.

### 8b. Create the tunnel

```bash
cloudflared tunnel create zettair-search
```

Note the **tunnel ID** printed (e.g. `c34ed65f-88f8-48b1-88ea-45792e51a5a6`). Credentials are saved to `~/.cloudflared/<tunnel-id>.json`.

### 8c. Create the config file

```bash
cat > ~/.cloudflared/config.yml << EOF
tunnel: <YOUR-TUNNEL-ID>
credentials-file: /Users/<YOUR-USERNAME>/.cloudflared/<YOUR-TUNNEL-ID>.json

ingress:
  - hostname: search.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
EOF
```

### 8d. Add the DNS record

```bash
cloudflared tunnel route dns zettair-search search.yourdomain.com
```

This creates a CNAME in Cloudflare DNS automatically.

### 8e. Start the tunnel

```bash
cloudflared tunnel run zettair-search
```

### 8f. Run tunnel as a background service (auto-start on boot)

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

Your search engine is now live at `https://search.yourdomain.com`.

---

## Step 9 — Auto-start the search server on boot

Create a launchd agent so the server starts automatically:

```bash
cat > ~/Library/LaunchAgents/com.zettair.search.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.zettair.search</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/YOUR_USERNAME/search/zettair-search/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ZET_BINARY</key>
    <string>/Users/YOUR_USERNAME/search/zettair/devel/zet</string>
    <key>ZET_INDEX</key>
    <string>/Users/YOUR_USERNAME/search/zettair/wikiindex/index</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>/Users/YOUR_USERNAME/search/zettair-search</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/zettair.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/zettair.err</string>
</dict>
</plist>
EOF

# Replace YOUR_USERNAME, then load it:
launchctl load ~/Library/LaunchAgents/com.zettair.search.plist
```

---

## Checkpoints (git tags)

| Tag | Description |
|-----|-------------|
| `checkpoint-0` | Working engine, basic UI |
| `checkpoint-1` | Snippets, images, instant search, clickable links |
| `checkpoint-1b` | Google-style result cards (current) |

Roll back with: `git checkout checkpoint-0`

---

## Directory structure

```
zettair-search/
  server.py          ← FastAPI server (image proxy, sidecar loading, zet wrapper)
  index.html         ← Single-file frontend (no build step)
  prd/               ← Product requirement docs for each feature
  README.md          ← This file

zettair/
  devel/
    zet              ← Compiled binary (gitignored — build from source)
    src/             ← Patched C source (ARM fixes, strvlen inline)
  wikipedia/
    wiki2trec.py     ← Converts XML dump → TREC + snippets/images JSON
    simplewiki.xml   ← Wikipedia dump (gitignored — download fresh)
    simplewiki.trec  ← TREC format (gitignored — regenerate)
    simplewiki_snippets.json  ← (gitignored — regenerate)
    simplewiki_images.json    ← (gitignored — regenerate)
  wikiindex/
    index.*          ← Zettair index files (gitignored — regenerate)
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ZET_BINARY` | `../zettair/devel/zet` | Path to compiled zet binary |
| `ZET_INDEX` | `../zettair/wikiindex/index` | Path to Zettair index |
| `ZET_PORT` | `8765` | Port to listen on |
| `ZET_SNIPPETS` | `../zettair/wikipedia/simplewiki_snippets.json` | Snippets sidecar |
| `ZET_IMAGES` | `../zettair/wikipedia/simplewiki_images.json` | Images sidecar |

---

## Troubleshooting

**Server won't start — port in use:**
```bash
ps aux | grep server.py   # find the PID
kill <PID>
```

**Images not loading:**
Images are proxied through `/img?url=...` to avoid Wikimedia CDN blocking direct browser requests. If images break, check the server is running.

**Cloudflare tunnel disconnected:**
```bash
sudo launchctl start com.cloudflare.cloudflared
```

**Re-index after a new Wikipedia dump:**
```bash
cd ~/search/zettair/wikipedia
python3 wiki2trec.py          # regenerate TREC + sidecars
cd ../wikiindex
rm -f index.*
../devel/zet -i -f index ../wikipedia/simplewiki.trec
# restart server.py
```
