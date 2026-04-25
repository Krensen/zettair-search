# Zettair Wikipedia Search

A full-text BM25 search engine over Simple English Wikipedia (~256k articles), with query-biased summaries, click-prior ranking, and autosuggest from real Wikipedia clickstream data.

Live at: **https://zettair.io**

---

## What it is

- **Search engine:** [Zettair](https://github.com/Krensen/zettair) (BM25/Okapi), patched for ARM + click-prior ranking + JSON output
- **Backend:** FastAPI + persistent worker pool (50× lower latency than per-query subprocess)
- **Frontend:** Single-file Google-style HTML/JS — autosuggest, knowledge panel, thumbnails, query-term highlighting
- **Summaries:** Query-biased snippet generation (port of Turpin, Hawking & Williams, SIGIR 2003)
- **Autosuggest:** 152k queries ranked by 15 months of Wikipedia clickstream data (decay-weighted)
- **Click prior:** Per-article click scores baked into BM25 ranking inline (not post-ranking)

---

## Repos

| Repo | Contents |
|------|----------|
| [`Krensen/zettair`](https://github.com/Krensen/zettair) | Patched Zettair C source + Wikipedia pipeline scripts |
| [`Krensen/zettair-search`](https://github.com/Krensen/zettair-search) | FastAPI server, frontend, deploy scripts, PRDs |

---

## Quick start (fresh VPS)

```bash
# Clone the service repo and run the one-time setup script
git clone https://github.com/Krensen/zettair-search.git
bash zettair-search/deploy/setup.sh
```

`setup.sh` handles everything: installs deps, clones both repos, builds the binary, downloads and indexes Wikipedia, builds all data files, installs systemd services. See below for details.

---

## Full setup walkthrough

### Prerequisites

- Ubuntu 24.04 (ARM64 or x86_64) — tested on Hetzner CAX21 (ARM, €5.49/mo)
- ~25GB free disk space
- ~60–90 min for full setup (mostly downloads + indexing)

### Step 1 — Install dependencies

```bash
apt-get update && apt-get install -y \
    python3 python3-pip git gcc make autoconf automake libtool libz-dev curl wget bzip2
pip3 install fastapi uvicorn
```

### Step 2 — Clone repos

```bash
cd /opt
git clone https://github.com/Krensen/zettair-search.git
git clone https://github.com/Krensen/zettair.git
```

### Step 3 — Build Zettair

```bash
cd /opt/zettair/devel

# ARM64 (Hetzner CAX, Apple Silicon)
./configure --build=aarch64-unknown-linux-gnu   # Linux ARM
./configure --build=arm-apple-darwin            # macOS ARM

# x86_64
./configure

make
```

Binary: `devel/zet`

### Step 4 — Download Wikipedia dump

```bash
cd /opt/zettair/wikipedia
wget -O simplewiki.xml.bz2 \
    https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2
bunzip2 simplewiki.xml.bz2
mv simplewiki-latest-pages-articles.xml simplewiki.xml
```

~330MB download, ~1.5GB decompressed.

### Step 5 — Convert to TREC + extract sidecars

```bash
python3 wiki2trec.py simplewiki.xml simplewiki.trec
```

Produces:
- `simplewiki.trec` — Zettair input (~406MB)
- `simplewiki_snippets.json` — pre-baked fallback snippets (~87MB)
- `simplewiki_images.json` — Wikimedia image URLs (~9MB)

Takes ~5 min.

### Step 6 — Build search index

```bash
mkdir -p /opt/zettair/wikiindex
cd /opt/zettair/wikiindex
../devel/zet -i --okapi -f index ../wikipedia/simplewiki.trec
```

Takes ~5–20 min. Produces `wikiindex/index.*`.

### Step 7 — Download clickstream data

```bash
cd /opt/zettair/wikipedia
python3 refresh_clickstream.py
```

Downloads 15 months of English Wikipedia clickstream (~6.5GB total). Rate-limited to be polite to Wikimedia.

### Step 8 — Build docno map

```bash
python3 build_docno_map.py
```

Produces `docno_map.tsv` — maps sequential Zettair docno integers to article titles. ~1 min.

### Step 9 — Build click prior

```bash
python3 build_click_prior.py
```

Aggregates 15 months of clickstream with exponential decay into `click_prior.bin` — a float32 array indexed by docno. ~4 min.

The click prior is loaded by Zettair at startup and applied inline during BM25 scoring (not post-ranking). Strength controlled by `ZET_CLICK_ALPHA` (default 0.5).

### Step 10 — Build autosuggest index

```bash
python3 build_autosuggest.py
```

Produces `autosuggest.json` — 152k (query, count) pairs sorted for binary search. Fired after 2 chars, 150ms debounce, 8 suggestions max. ~10 min.

### Step 11 — Build docstore

```bash
python3 build_docstore.py
```

Produces:
- `simplewiki.docstore` — full article text, concatenated (~350MB)
- `simplewiki.docmap` — byte offsets for O(1) random access by docno (~9MB)

Used by the query-biased summariser (`summarise.py`) to generate snippets at query time. ~30 sec.

### Step 12 — Start the server

```bash
cd /opt/zettair-search

ZET_BINARY=/opt/zettair/devel/zet \
ZET_INDEX=/opt/zettair/wikiindex/index \
ZET_CLICK_PRIOR=/opt/zettair/wikipedia/click_prior.bin \
ZET_CLICK_ALPHA=0.5 \
ZET_WORKERS=2 \
ZET_SUMMARISE=1 \
ZET_DOCSTORE=/opt/zettair/wikipedia/simplewiki.docstore \
ZET_DOCMAP=/opt/zettair/wikipedia/simplewiki.docmap \
python3 server.py
```

Test: `curl http://localhost:8765/search?q=london`

On first start, loads ~100MB of sidecar data into memory (~10 sec).

### Step 13 — Systemd services

```bash
cp deploy/zettair-search.service /etc/systemd/system/
cp deploy/cloudflared.service    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zettair-search cloudflared
```

### Step 14 — Cloudflare tunnel

```bash
# On your local machine:
cloudflared tunnel login
cloudflared tunnel create zettair-search
cloudflared tunnel route dns zettair-search search.yourdomain.com

# Copy credentials to VPS:
scp ~/.cloudflared/<tunnel-id>.json root@VPS_IP:/etc/cloudflared/

# On VPS, create config:
cat > /etc/cloudflared/config.yml << EOF
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
protocol: http2
ingress:
  - hostname: search.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
EOF

systemctl restart cloudflared
```

---

## CI/CD

Every push to `main` on `zettair-search` triggers a GitHub Actions workflow that SSHs to the VPS and runs `deploy/deploy.sh` (git pull + pip install + systemctl restart).

Setup:
1. Generate a deploy key: `ssh-keygen -t ed25519 -f deploy_key`
2. Add `deploy_key.pub` to the VPS: `~/.ssh/authorized_keys` (for a `deploy` user)
3. Add `deploy_key` (private) as GitHub secret `VPS_SSH_KEY`
4. Add VPS IP as secret `VPS_IP`

See `.github/workflows/deploy.yml`.

---

## Monthly clickstream refresh

A cron job runs `refresh_clickstream.py` daily from the 10th–28th of each month. When a new month's data is available it downloads it, rebuilds `autosuggest.json` and `click_prior.bin`, and sends a Telegram notification.

```bash
# Crontab entry (adjust path):
0 6 10-28 * * /usr/bin/python3 /opt/zettair/wikipedia/refresh_clickstream.py
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ZET_BINARY` | `../zettair/devel/zet` | Path to compiled zet binary |
| `ZET_INDEX` | `../zettair/wikiindex/index` | Path to Zettair index |
| `ZET_PORT` | `8765` | HTTP port |
| `ZET_WORKERS` | `2` | Persistent zet worker processes |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout (seconds) |
| `ZET_CLICK_PRIOR` | `../zettair/wikipedia/click_prior.bin` | Click prior binary |
| `ZET_CLICK_ALPHA` | `0.5` | Click boost strength (0 = off, 1.5 = strong) |
| `ZET_SUMMARISE` | `0` | Enable query-biased summaries (`1` to enable) |
| `ZET_DOCSTORE` | `../zettair/wikipedia/simplewiki.docstore` | Full-text docstore |
| `ZET_DOCMAP` | `../zettair/wikipedia/simplewiki.docmap` | Docstore offset map |
| `ZET_SNIPPETS` | `../zettair/wikipedia/simplewiki_snippets.json` | Fallback snippets |
| `ZET_IMAGES` | `../zettair/wikipedia/simplewiki_images.json` | Image URLs |
| `ZET_AUTOSUGGEST` | `../zettair/wikipedia/autosuggest.json` | Autosuggest data |
| `ZET_SUMM_TIMEOUT` | `2.0` | Summariser timeout (seconds) |

---

## Architecture

```
Browser
  │  HTTPS
  ▼
Cloudflare edge
  │  HTTP/2 tunnel
  ▼
cloudflared (VPS)
  │  localhost
  ▼
server.py (FastAPI)  ← loads snippets, images, autosuggest, docstore into RAM
  │
  ├── ZetPool (2 workers) ─── zet binary (index memory-mapped)
  │     stdin: query text
  │     stdout: JSON Lines results
  │
  └── SummarisePool (1 worker) ─── summarise.py
        stdin: {terms, docs} JSON
        stdout: {summaries} JSON
```

---

## Git tags

| Tag | Description |
|-----|-------------|
| `checkpoint-0` | Working engine, basic UI |
| `checkpoint-1` | Snippets, images, Google-style cards |
| `checkpoint-2` | Query/click logging, daily digest |
| `checkpoint-3` | Autosuggest live |
| `checkpoint-4` | Paul Smith styling, multi-month clickstream |
| `checkpoint-5` | Click prior live (α=0.5) |
| `checkpoint-6` | Persistent worker pool (PRD-007, 50× speedup) |
| `checkpoint-7` | Query-biased summaries (PRD-008) |

---

## Troubleshooting

**Server won't start:**
```bash
journalctl -u zettair-search -n 50
```

**Summariser slow or falling back to pre-baked snippets:**
Check `ZET_SUMMARISE=1` is set and `simplewiki.docstore` exists.

**Autosuggest empty:**
Rebuild: `python3 /opt/zettair/wikipedia/build_autosuggest.py`

**Cloudflare tunnel drops:**
```bash
systemctl restart cloudflared
journalctl -u cloudflared -n 30
```

**Re-index after new Wikipedia dump:**
```bash
cd /opt/zettair/wikipedia
python3 wiki2trec.py simplewiki.xml simplewiki.trec
cd /opt/zettair/wikiindex && rm -f index.*
../devel/zet -i --okapi -f index ../wikipedia/simplewiki.trec
python3 build_docno_map.py
python3 build_click_prior.py
python3 build_autosuggest.py
python3 build_docstore.py
systemctl restart zettair-search
```

---

## Full English Wikipedia (future)

When ready to index all ~6.7M English Wikipedia articles:
1. Resize VPS disk to 160GB+ (Hetzner: online, no downtime)
2. Upgrade to a box with 8–16GB RAM
3. Download `enwiki-latest-pages-articles.xml.bz2` (~23GB)
4. Run the same pipeline — `wiki2trec.py`, `zet -i`, build sidecars
5. Update `ZET_*` env vars to point at new paths, restart

Code doesn't change. The sidecar JSON loading may need to be redesigned for the full corpus (SQLite or mmap instead of loading into RAM).
