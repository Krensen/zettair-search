# Zettair Wikipedia Search

A full-text BM25 search engine over Simple English Wikipedia, running locally on a Mac with a permanent public URL via Cloudflare Tunnel.

**Live demo:** https://search.hughwilliams.com

---

## What it is

- **Search engine:** Zettair (BM25/Okapi), patched for Apple Silicon
- **Backend:** FastAPI Python server wrapping the `zet` CLI
- **Frontend:** Single-file Google-style HTML/JS UI with Paul Smith styling, autosuggest, knowledge panel, image thumbnails
- **Data:** Simple English Wikipedia (~256k articles, ~64k images)
- **Autosuggest:** 152k queries derived from Wikipedia clickstream (15 months, decay-weighted)
- **Tunnel:** Cloudflare named tunnel → permanent public HTTPS URL

---

## Prerequisites

- macOS (Apple Silicon or Intel)
- Homebrew
- A Cloudflare account with your domain's nameservers pointed at Cloudflare
- ~12GB free disk space (XML dump + index + clickstream files)
- ~60 min for full setup (most of it is downloads)

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

Binary will be at `~/search/zettair/devel/zet`.

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

Produces:
- `simplewiki.trec` — Zettair input format
- `simplewiki_snippets.json` — clean prose snippets per article
- `simplewiki_images.json` — Wikimedia CDN image URLs per article
- `simplewiki_titles.txt` — article title list (used by autosuggest pipeline)

Takes ~5–10 minutes.

---

## Step 6 — Index with Zettair

```bash
mkdir -p ~/search/zettair/wikiindex
cd ~/search/zettair/wikiindex

../devel/zet -i -f index ../wikipedia/simplewiki.trec
```

Takes ~5 minutes. Produces `index.*` files in `wikiindex/`.

---

## Step 7 — Download Wikipedia clickstream data (autosuggest)

This gives you real search popularity data for 256k articles.

```bash
cd ~/search/zettair/wikipedia

# Download 15 months of English Wikipedia clickstream (~6.5GB total)
# Do these sequentially to avoid Wikimedia rate limiting
for month in 2024-01 2024-02 2024-03 2024-04 2024-05 2024-06 \
             2024-07 2024-08 2024-09 2024-10 2024-11 2024-12 \
             2025-01 2025-02 2025-03; do
  f="clickstream-enwiki-${month}.tsv.gz"
  echo "Downloading $month..."
  curl -O "https://dumps.wikimedia.org/other/clickstream/${month}/${f}"
  sleep 2
done
```

Takes ~30–45 minutes depending on your connection.

---

## Step 8 — Build autosuggest index

```bash
cd ~/search/zettair/wikipedia
python3 build_autosuggest.py
```

Produces `autosuggest.json` — 152k query+popularity pairs, sorted for binary search. Takes ~10 minutes (processes all 15 months with decay weighting).

---

## Step 9 — Build click prior (improves ranking of popular articles)

```bash
cd ~/search/zettair/wikipedia
python3 build_docno_map.py   # ~1 min — maps docno → title
python3 build_click_prior.py # ~4 min — aggregates clickstream into click_prior.bin
```

Produces:
- `docno_map.tsv` — sequential docno → article title mapping
- `click_prior.bin` — float32 array of decayed click scores per article (~1MB)

---

## Step 10 — Run the search server

```bash
cd ~/search/zettair-search

ZET_BINARY=~/search/zettair/devel/zet \
ZET_INDEX=~/search/zettair/wikiindex/index \
python3 server.py
```

The click prior is loaded automatically if `click_prior.bin` is present at the default path. Tune with:
```bash
ZET_CLICK_ALPHA=0.5  # default — higher = stronger click boost
```

Test locally: http://localhost:8765

The server loads snippets, images, and autosuggest into memory on startup (~10 seconds).

---

## Step 11 — Set up Cloudflare Tunnel (permanent public URL)

### 10a. Log in to Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser. Authorise your domain. A cert is saved to `~/.cloudflared/cert.pem`.

### 10b. Create the tunnel

```bash
cloudflared tunnel create zettair-search
```

Note the **tunnel ID** printed. Credentials saved to `~/.cloudflared/<tunnel-id>.json`.

### 10c. Create the config file

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

### 10d. Add the DNS record

```bash
cloudflared tunnel route dns zettair-search search.yourdomain.com
```

### 10e. Run tunnel as a background service

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

Your search engine is now live at `https://search.yourdomain.com`.

---

## Step 12 — Auto-start the search server on boot

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

# Replace YOUR_USERNAME throughout, then:
launchctl load ~/Library/LaunchAgents/com.zettair.search.plist
```

---

## Step 13 — Set up monthly clickstream refresh (auto-updates autosuggest)

A cron job checks for new clickstream data from the 10th of each month, downloads it, rebuilds `autosuggest.json` automatically.

Configure this via your OpenClaw instance:
```
Run daily from 10th: python3 ~/search/zettair/wikipedia/refresh_clickstream.py
```

Or add a crontab entry:
```bash
# Runs daily at 6am from the 10th–28th of each month
0 6 10-28 * * python3 /Users/YOUR_USERNAME/search/zettair/wikipedia/refresh_clickstream.py
```

---

## Checkpoints (git tags)

| Tag | Description |
|-----|-------------|
| `checkpoint-0` | Working engine, basic UI |
| `checkpoint-1` | Snippets, images, clickable links |
| `checkpoint-1b` | Google-style result cards |
| `checkpoint-2` | Query + click logging, daily digest |
| `checkpoint-3` | Autosuggest live |
| `checkpoint-4` | Paul Smith styling, multi-month clickstream, PRD-006 spec |
| `checkpoint-5` | Click prior live (α=0.5), persistent process spec (PRD-007) ← current |

Roll back with: `git checkout checkpoint-X`

---

## Directory structure

```
zettair-search/
  server.py          ← FastAPI server (search, image proxy, autosuggest, logging)
  index.html         ← Single-file frontend (no build step)
  digest.py          ← Daily query digest script
  logs/              ← queries.jsonl, clicks.jsonl, clickstream_refresh.jsonl
  prd/               ← Product requirement docs
  README.md          ← This file

zettair/
  devel/
    zet              ← Compiled binary (gitignored — build from source)
    src/             ← Patched C source (ARM fixes, strvlen inline)
  wikipedia/
    wiki2trec.py          ← XML dump → TREC + snippets/images JSON
    build_autosuggest.py      ← Clickstream → autosuggest.json (with decay)
    build_docno_map.py        ← TREC file → docno_map.tsv
    build_click_prior.py      ← Clickstream → click_prior.bin (float32, decay-weighted)
    refresh_clickstream.py    ← Monthly auto-download + rebuild
    simplewiki.xml            ← Wikipedia dump (gitignored)
    simplewiki.trec        ← TREC format (gitignored)
    simplewiki_snippets.json  ← (gitignored)
    simplewiki_images.json    ← (gitignored)
    simplewiki_titles.txt     ← (gitignored)
    autosuggest.json              ← (gitignored)
    docno_map.tsv                 ← (gitignored)
    click_prior.bin               ← (gitignored)
    clickstream-enwiki-*.tsv.gz   ← (gitignored — ~6.5GB)
  wikiindex/
    index.*          ← Zettair index files (gitignored)
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
| `ZET_AUTOSUGGEST` | `../zettair/wikipedia/autosuggest.json` | Autosuggest data |
| `ZET_QUERY_LOG` | `logs/queries.jsonl` | Query log path |
| `ZET_CLICK_LOG` | `logs/clicks.jsonl` | Click log path |
| `ZET_CLICK_PRIOR` | `../zettair/wikipedia/click_prior.bin` | Click prior binary (auto-detected) |
| `ZET_CLICK_ALPHA` | `0.5` | Click boost strength (0 = disabled) |

---

## Troubleshooting

**Server won't start — port in use:**
```bash
lsof -ti:8765 | xargs kill
```

**Images not loading:**
Images are proxied through `/img?url=...` — Wikimedia CDN blocks direct browser requests. If images break, check the server is running.

**Autosuggest not working:**
Check `autosuggest.json` exists. Rebuild with `python3 build_autosuggest.py`.

**Cloudflare tunnel disconnected:**
```bash
sudo launchctl start com.cloudflare.cloudflared
```

**Re-index after a new Wikipedia dump:**
```bash
cd ~/search/zettair/wikipedia
python3 wiki2trec.py
cd ../wikiindex && rm -f index.*
../devel/zet -i -f index ../wikipedia/simplewiki.trec
python3 ../wikipedia/build_autosuggest.py  # rebuild autosuggest too
# restart server.py
```
