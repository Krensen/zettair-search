# Zettair Search

A full-text BM25 search engine over the top 1,000,000 English Wikipedia articles, with query-biased summaries, click-prior ranking, and autosuggest — all from real Wikipedia clickstream data.

Live at: **https://zettair.io**

---

## What it is

A production search engine built on [Zettair](https://github.com/rmit-ir/zettair), a research-grade BM25 engine from RMIT. The interesting parts are the layer on top of it:

- **Click-prior ranking** — 15 months of Wikipedia clickstream data is decay-weighted and baked into BM25 scoring at query time (not post-ranking). Articles that real users actually click on rank higher.
- **Query-biased summaries** — Zettair's C summariser (Turpin, Hawking & Williams, SIGIR 2003) generates snippets that highlight the query terms in context, not just the first sentence.
- **Autosuggest** — 690k queries ranked by clickstream popularity, served via binary search in ~1ms.
- **Persistent worker pool** — 2 long-lived `zet` processes with the index memory-mapped. Queries arrive via stdin, results come back as JSON Lines. ~50× lower latency than spawning a process per query.
- **Disk-based sidecar stores** — snippets and images are stored as flat binary files with a JSON offset map. `os.pread()` seeks to the exact byte range per result — nothing is loaded into RAM except the map (~50MB for 1M articles).

---

## Two repos

| Repo | What's in it |
|------|-------------|
| [`Krensen/zettair`](https://github.com/Krensen/zettair) | Patched Zettair C source, Wikipedia pipeline scripts |
| [`Krensen/zettair-search`](https://github.com/Krensen/zettair-search) | FastAPI server, frontend (`index.html`), deploy scripts, PRDs |

The C patches in `zettair` add: ARM build support, click-prior scoring (`okapi.c`), `summary` field in JSON output (`commandline.c`).

The pipeline scripts in `zettair/wikipedia/` produce all the data files the server needs at startup.

---

## Repository layout (`zettair-search`)

```
server.py          — FastAPI app: worker pool, FlatStore, search/suggest/click endpoints
index.html         — Single-file frontend (HTML + CSS + JS, no build step)
loadtest.py        — Load testing script with latency percentiles and histogram
deploy/
  setup.sh         — One-time VPS provisioning (install, build, download, index)
  deploy.sh        — Called by CI/CD on every push: git pull + restart
  zettair-search.service  — systemd unit for the search service
  cloudflared.service     — systemd unit for the Cloudflare tunnel
.github/workflows/deploy.yml  — GitHub Actions: SSH → deploy.sh on push to main
prd/               — Product requirements documents for each major feature
logs/              — queries.jsonl, clicks.jsonl, zet_crashes.jsonl (gitignored)
```

---

## How the server works

```
Browser (HTTPS)
  └─▶ Caddy (VPS, handles TLS + reverse proxy)
        └─▶ server.py :8765 (FastAPI / uvicorn)
              ├─▶ ZetPool — 2 persistent zet processes
              │     query via stdin → JSON Lines on stdout
              │     index is memory-mapped by the OS
              ├─▶ FlatStore (_snippets_store) — os.pread() into snippets binary
              ├─▶ FlatStore (_images_store)   — os.pread() into images binary
              └─▶ _autosuggest list           — sorted (query, count) pairs
```

**Query flow:**
1. `GET /search?q=einstein&n=10` arrives at `server.py`
2. A semaphore acquires one of the 2 `zet` workers
3. The query is written to the worker's stdin; JSON Lines are read from stdout
4. Each result line has `rank`, `docno`, `score`, `docid`, and `summary` (from Zettair's C summariser)
5. `enrich_results()` adds `snippet` (from `summary`, falling back to the pre-baked snippets store) and `image_url` (from the images store)
6. The response is returned; the worker is released back to the pool

**Click flow:**
`POST /click` logs `{ts, q, docno, rank, score}` to `logs/clicks.jsonl`. Used for future ranking improvement.

---

## Data files (on `/mnt/wikipedia-source/`)

All large files live on a separate Hetzner volume, not the boot disk.

| File | Size | What it is |
|------|------|------------|
| `enwiki_top1m.trec` | ~25 GB | 1M Wikipedia articles in TREC format (Zettair input) |
| `wikiindex/` | ~8 GB | Zettair inverted index |
| `enwiki_top1m_snippets.store` | ~360 MB | Pre-baked snippet text, concatenated UTF-8 |
| `enwiki_top1m_snippets.map` | ~50 MB | JSON: `{docno: [offset, length]}` |
| `enwiki_top1m_images.store` | ~60 MB | Wikimedia image URLs, concatenated |
| `enwiki_top1m_images.map` | ~10 MB | JSON: `{docno: [offset, length]}` |
| `click_prior.bin` | ~4 MB | float32 array indexed by Zettair docno |
| `autosuggest.json` | ~15 MB | Sorted `[[query, count], ...]` array |
| `docno_map.tsv` | ~20 MB | `internal_id\ttitle` — maps Zettair integers to article titles |

---

## Environment variables

Configured in `deploy/zettair-search.service`. Defaults shown:

| Variable | Default | Description |
|----------|---------|-------------|
| `ZET_BINARY` | `../zettair/devel/zet` | Path to compiled zet binary |
| `ZET_INDEX` | `../zettair/wikiindex/index` | Path to Zettair index |
| `ZET_PORT` | `8765` | HTTP port |
| `ZET_WORKERS` | `2` | Persistent zet worker processes |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout in seconds |
| `ZET_CLICK_PRIOR` | `../zettair/wikipedia/click_prior.bin` | Click prior float32 array |
| `ZET_CLICK_ALPHA` | `0.5` | Click boost strength (0 = off, 1.5 = strong) |
| `ZET_SNIPPETS_STORE` | `…enwiki_top1m_snippets.store` | Snippets flat binary |
| `ZET_SNIPPETS_MAP` | `…enwiki_top1m_snippets.map` | Snippets offset map |
| `ZET_IMAGES_STORE` | `…enwiki_top1m_images.store` | Images flat binary |
| `ZET_IMAGES_MAP` | `…enwiki_top1m_images.map` | Images offset map |
| `ZET_AUTOSUGGEST` | `…autosuggest.json` | Autosuggest sorted array |

---

## Fresh VPS setup

```bash
sudo bash deploy/setup.sh
```

`setup.sh` does everything in order, guarded by existence checks so it's safe to re-run after any failure:

1. Install system packages and Python deps
2. Create `deploy` and `zettair` users
3. Clone both repos into `/opt/`
4. Build the Zettair binary
5. Download the enwiki bz2 dump (~23 GB) to the volume
6. Download 15 months of clickstream data (~7.4 GB)
7. Run `select_top_articles.py` → `top_titles.txt` (top 1M by decayed click score)
8. Run `wiki2trec.py` with bz2 streaming and title allowlist (~4–8 hours)
9. Auto-delete bz2 if volume free space < 25 GB
10. Build docno map, click prior, autosuggest, docstore
11. Build the Zettair index (~30–60 min)
12. Set permissions, install and enable systemd service
13. Install cloudflared

**Prerequisites:**
- Ubuntu 24.04 ARM64 or x86_64
- Hetzner CCX13 (2 vCPU, 8 GB RAM) or equivalent
- 80 GB attached volume mounted at `/mnt/wikipedia-source/`
- ~8–12 hours total (mostly download + indexing)

Config at the top of `setup.sh`: `CORPUS_SIZE`, `CLICKSTREAM_MONTHS`, `DEPLOY_USER`, `SERVICE_USER`, `VOLUME`, etc.

---

## CI/CD

Every push to `main` triggers `.github/workflows/deploy.yml`, which SSHs to the VPS and runs `deploy/deploy.sh`:

```
git pull → pip install -r requirements.txt → systemctl restart → health check
```

**One-time setup:**
1. `ssh-keygen -t ed25519 -f deploy_key`
2. Add `deploy_key.pub` to `~/.ssh/authorized_keys` on the VPS (as `deploy` user)
3. Add `deploy_key` as GitHub secret `VPS_SSH_KEY`
4. Add VPS IP as GitHub secret `VPS_IP`

**Reverse proxy:** Caddy is installed separately on the VPS and handles TLS termination and reverse proxying to `:8765`. It is not managed by this repo. Caddy sets `X-Forwarded-For` with the real client IP, which `server.py` reads for logging.

---

## Users and permissions

| User | Purpose | Owns |
|------|---------|------|
| `deploy` | Git pulls, deploys, admin | `/opt/zettair-search`, `/opt/zettair` |
| `zettair` | Runs the service | `/mnt/wikipedia-source/` |

Both repos are world-readable so the `zettair` service user can read `server.py` and the `zet` binary without owning them. The volume is `750` (zettair only) since the service is the only runtime reader.

`git pull` and `sudo systemctl restart zettair-search` — no sudo needed for the pull.

---

## Load testing

```bash
# 10 min soak test, 10 concurrent workers, queries weighted by click popularity
python3 loadtest.py --duration 600 --workers 10

# Against the live site
python3 loadtest.py --url https://zettair.io --duration 120 --workers 4
```

Fetches ~38k real queries from `/suggest` (weighted by click count), fires them concurrently, reports mean/p50/p75/p90/p95/p99/max and a latency histogram.

Baseline on CCX13 (2 vCPU, 8 GB RAM): ~30 req/s, p50 ~250ms, p95 ~500ms, p99 ~700ms.

---

## Corpus refresh

**Clickstream** (monthly): download new `clickstream-enwiki-YYYY-MM.tsv.gz`, rebuild `click_prior.bin` and `autosuggest.json`. Triggered manually for now.

**Full corpus rebuild** (quarterly): re-run `setup.sh` from step 7 onwards. Increase `CORPUS_SIZE` at the top of `setup.sh` if you want more articles — bumping to 1.5M is a matter of changing one number and re-running.

---

## Troubleshooting

**Service won't start / crashes:**
```bash
sudo journalctl -u zettair-search -n 50
```

**`zet` crashes with `docmap_load: Assertion fd >= 0`:**
Index files are probably owned by root. Fix: `sudo chown -R zettair:zettair /mnt/wikipedia-source/`

**`git pull` fails with permission denied:**
`.git` was written by root. Fix: `sudo chown -R deploy:deploy /opt/zettair-search`

**Autosuggest returns nothing:**
```bash
sudo -u zettair curl 'http://localhost:8765/suggest?q=ei&n=5'
# if empty, rebuild: cd /opt/zettair/wikipedia && python3 build_autosuggest.py
```

**Cloudflare tunnel drops:**
```bash
sudo systemctl restart cloudflared
sudo journalctl -u cloudflared -n 30
```

---

## PRD index

Design decisions are recorded in `prd/`. Reading order if you're new to the codebase:

| PRD | What it covers |
|-----|---------------|
| PRD-006 | Click-prior ranking — how clickstream data is baked into BM25 |
| PRD-007 | Persistent worker pool — why and how the zet process pool works |
| PRD-011 | C summariser — replacing the Python summariser with Zettair's built-in |
| PRD-012 | Top-1M corpus — why 1M articles, disk budget, build pipeline |
| PRD-013 | Session logging — session IDs, IP logging, result lists in query log |
