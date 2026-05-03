# Zettair Search

A full-text BM25 search engine over the top 1.5 million English Wikipedia articles, with field-weighted ranking, query-biased summaries, click-prior ranking, and autosuggest — all from real Wikipedia clickstream data.

Live at: **https://zettair.io**

---

## What it is

A production search engine built on [Zettair](https://github.com/rmit-ir/zettair), a research-grade BM25 engine from RMIT. The interesting parts are the layer on top of it:

- **Field-weighted BM25** — title term occurrences contribute more to the score than body occurrences via a 4-bit field-id encoded in each posting offset. Tunable per-field via env vars (`ZET_BOOST_TITLE` etc). Generalises to up to 16 fields without a format change.
- **Click-prior ranking** — 15 months of Wikipedia clickstream data is decay-weighted and added to BM25 scoring. Articles that real users actually click on rank higher.
- **Query-biased summaries** — Python summariser ported from the Turpin/Hawking/Williams SIGIR 2003 algorithm, called inline by the FastAPI server. Reads cleaned article text from a disk-resident docstore.
- **Autosuggest** — ~1M queries ranked by clickstream popularity, served via binary search in ~1 ms.
- **Persistent worker pool** — 2 long-lived `zet` processes with the index memory-mapped. Queries arrive via stdin, results come back as JSON Lines. ~50× lower latency than spawning a process per query.
- **Disk-based sidecar stores** — snippets, images, URLs and the document text are stored as flat binary files with a JSON offset map. `os.pread()` seeks to the exact byte range per result; only the offset maps stay in RAM.

---

## Two repos

| Repo | What's in it |
|------|-------------|
| [`Krensen/zettair`](https://github.com/Krensen/zettair) | Patched Zettair C source, Wikipedia pipeline scripts |
| [`Krensen/zettair-search`](https://github.com/Krensen/zettair-search) | FastAPI server, frontend (`index.html`), deploy scripts, PRDs |

The C patches in `zettair`:
- ARM64 build support
- Click-prior scoring (`okapi.c`)
- `summary` field in JSON output (`commandline.c`) — currently unused but harmless
- 64-bit byte offsets in the docmap (the upstream RMIT version overflowed at 4 GB; we hit it on 1.5M corpus)
- 4-bit field-id in each posting offset (PRD-017) for field-weighted ranking
- All non-Okapi rankers removed (cosine, dirichlet, hawkapi, impact-ordered) — ~8,300 lines of dead code

The pipeline scripts in `zettair/wikipedia/` produce the data files the server needs at startup.

---

## Repository layout (`zettair-search`)

```
server.py          — FastAPI app: worker pool, FlatStore, summariser, search/suggest/click endpoints
summarise.py       — Inline Python query-biased summariser (PRD-016)
index.html         — Single-file frontend (HTML + CSS + JS, no build step)
loadtest.py        — Load testing with latency percentiles and histogram
digest.py          — Daily query digest (reads logs/queries.jsonl, sends Telegram summary)
requirements.txt   — Python deps: fastapi, uvicorn[standard]
deploy/
  setup.sh                  — One-time VPS provisioning (install, build, download, index)
  deploy.sh                 — Called by CI/CD on every push: git pull + restart
  zettair-search.service    — systemd unit for the search service
  cloudflared.service       — legacy systemd unit, kept for reference, not used
.github/workflows/deploy.yml — GitHub Actions: SSH → deploy.sh on push to main
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
              │     ZET_BOOST_TITLE etc applied at score time
              ├─▶ FlatStore (_docstore)        — os.pread() for the inline summariser
              ├─▶ FlatStore (_snippets_store)  — pre-baked snippet fallback
              ├─▶ FlatStore (_images_store)    — Wikimedia image URLs
              ├─▶ FlatStore (_urls_store)      — canonical en.wikipedia.org URLs
              └─▶ _autosuggest list            — sorted (query, count) pairs
```

**Query flow:**
1. `GET /search?q=einstein&n=10` arrives at `server.py`
2. A semaphore acquires one of the 2 `zet` workers
3. The query is written to the worker's stdin; JSON Lines are read from stdout
4. Each result line has `rank`, `docno`, `score`, `docid`
5. `enrich_results()` per result:
   - reads the article text from `_docstore` and runs `summarise.summarise_doc()` to produce a snippet (falls back to the pre-baked snippet store if docstore lookup misses)
   - looks up the canonical URL in `_urls_store` (or builds it from the docno)
   - looks up the image URL in `_images_store`
6. The response is returned; the worker is released back to the pool

**Click flow:**
`POST /click` logs `{ts, q, docno, rank, score}` to `logs/clicks.jsonl`. Used as input for ranking experiments.

---

## Data files (on `/mnt/wikipedia-source/`)

All large files live on a separate Hetzner volume, not the boot disk.

| File | Size | What it is |
|------|------|------------|
| `enwiki_top1m.trec` | ~30 GB | 1.5M Wikipedia articles in TREC format with `<TITLE>` tags (Zettair input) |
| `wikiindex/` | ~12 GB | Zettair inverted index (postings encode 4-bit field-id per offset, may split across `index.v.0`/`v.1` if > 4 GB) |
| `enwiki_top1m_snippets.store` | ~540 MB | Pre-baked snippet text, concatenated UTF-8 |
| `enwiki_top1m_snippets.map` | ~70 MB | JSON: `{docno: [offset, length]}` |
| `enwiki_top1m_images.store` | ~120 MB | Wikimedia image URLs, concatenated |
| `enwiki_top1m_images.map` | ~30 MB | JSON: `{docno: [offset, length]}` |
| `enwiki_top1m_urls.store` | ~18 MB | Canonical URLs for ~330k articles whose dbkey ≠ safe_id |
| `enwiki_top1m_urls.map` | ~14 MB | URL offset map |
| `enwiki_top1m.docstore` | ~13 GB | Cleaned article text used by the inline summariser |
| `enwiki_top1m.docmap` | ~57 MB | Docstore offset map |
| `click_prior.bin` | ~6 MB | float32 array indexed by Zettair docno |
| `autosuggest.json` | ~27 MB | Sorted `[[query, count], ...]` array |

(`docno_map.tsv`, ~25 MB, lives in `/opt/zettair/wikipedia/` — only used at build time.)

The filename prefix `enwiki_top1m` is historical; the corpus is 1.5M.

---

## Environment variables

Configured in `deploy/zettair-search.service`. Values shown match what's deployed:

| Variable | Value | Description |
|----------|-------|-------------|
| `ZET_BINARY` | `/opt/zettair/devel/zet` | Path to compiled zet binary |
| `ZET_INDEX` | `/mnt/wikipedia-source/wikiindex/index` | Path to Zettair index |
| `ZET_PORT` | `8765` (default) | HTTP port |
| `ZET_WORKERS` | `2` | Persistent zet worker processes |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout in seconds |
| `ZET_CLICK_PRIOR` | `…/click_prior.bin` | Click prior float32 array |
| `ZET_CLICK_ALPHA` | `0.5` | Click boost strength (0 = off, 1.5 = strong) |
| `ZET_BOOST_TITLE` | `3.0` | Per-occurrence boost for title-tagged terms in BM25 |
| `ZET_BOOST_CAPTION` | `1.0` | Reserved (no `<CAPTION>` emitted yet) |
| `ZET_BOOST_CATEGORY` | `1.0` | Reserved |
| `ZET_BOOST_SEEALSO` | `1.0` | Reserved |
| `ZET_BOOST_INFOBOX` | `1.0` | Reserved |
| `ZET_SNIPPETS_STORE` | `…_snippets.store` | Pre-baked snippets (fallback for the summariser) |
| `ZET_SNIPPETS_MAP` | `…_snippets.map` | Snippets offset map |
| `ZET_IMAGES_STORE` | `…_images.store` | Wikimedia image URLs |
| `ZET_IMAGES_MAP` | `…_images.map` | Images offset map |
| `ZET_URLS_STORE` | `…_urls.store` | Canonical en.wikipedia.org URLs |
| `ZET_URLS_MAP` | `…_urls.map` | URLs offset map |
| `ZET_DOCSTORE` | `…enwiki_top1m.docstore` | Cleaned article text (read by inline summariser) |
| `ZET_DOCMAP` | `…enwiki_top1m.docmap` | Docstore offset map |
| `ZET_AUTOSUGGEST` | `…autosuggest.json` | Autosuggest sorted array |

---

## Fresh VPS setup

```bash
sudo bash deploy/setup.sh
```

`setup.sh` runs all steps in order, guarded by existence checks so it's safe to re-run after any failure:

1. Install system packages and Python deps
2. Create `deploy` and `zettair` users (if absent)
3. Verify the volume is mounted at `/mnt/wikipedia-source/` and chown to zettair
4. Clone both repos into `/opt/`, chown to deploy, world-readable
5. Build the Zettair binary (with ARM detection); install ELF binary to `/opt/zettair/devel/zet`, link `libzet.so` into `/usr/local/lib`
6. Download the enwiki bz2 dump (~24 GB) to the volume
7. Download 15 months of clickstream data (~7.4 GB) into `/opt/zettair/wikipedia/`
8. Run `select_top_articles.py` → `top_titles.txt` (top 1.5M by decayed click score)
9. Run `wiki2trec.py` with bz2 streaming and title allowlist (~6 hours streaming the bz2)
10. Auto-delete bz2 if volume free space drops below threshold
11. Build docno map, click prior, autosuggest, docstore, URL store
12. Build the Zettair index (~10 min for 1.5M articles)
13. Install systemd service
14. Verify ownership; report any root-owned files

(Caddy reverse proxy is installed separately — not managed by this script.)

**Prerequisites:**
- Ubuntu 24.04 ARM64 or x86_64
- Hetzner CCX13 (2 vCPU, 8 GB RAM) or equivalent
- 80 GB attached volume mounted at `/mnt/wikipedia-source/`
- ~8–12 hours total wall time, mostly the wiki2trec.py bz2 stream

Config at the top of `setup.sh`: `CORPUS_SIZE` (default 1500000), `CLICKSTREAM_MONTHS`, `DEPLOY_USER`, `SERVICE_USER`, `VOLUME`, `ENWIKI_DUMP_URL`, `BZ2_DELETE_THRESHOLD_GB`.

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

CI deploys are code-only. They don't rebuild the index or the zet binary. Index/binary changes need a manual `setup.sh` run on the server.

---

## Users and permissions

| User | Purpose | Owns |
|------|---------|------|
| `deploy` | Git pulls, deploys, admin | `/opt/zettair-search`, `/opt/zettair` |
| `zettair` | Runs the service | `/mnt/wikipedia-source/` (mode 750) |

Both repos are world-readable (chmod `o+rX`) so the `zettair` service user can read `server.py` and the `zet` binary without owning them. The volume is owned by zettair exclusively since the service is the only runtime reader.

`git pull` (no sudo) and `sudo systemctl restart zettair-search` is the standard deploy flow.

---

## Load testing

```bash
# 10 min soak test, 10 concurrent workers, queries weighted by click popularity
python3 loadtest.py --duration 600 --workers 10

# Against the live site
python3 loadtest.py --url https://zettair.io --duration 120 --workers 4
```

Fetches ~38k real queries from `/suggest` (weighted by click count), fires them concurrently, reports mean/p50/p75/p90/p95/p99/max and a latency histogram.

Baseline on CCX13 (2 vCPU, 8 GB RAM), 1.5M corpus, 4 workers, 10 concurrent clients:
~17 req/s, p50 ~500 ms, p95 ~1050 ms, p99 ~1500 ms.

(The earlier 1M corpus on the same box was ~30 req/s, p50 ~250 ms. The 1.5M
upgrade roughly halved throughput because the bigger index fits less well in
the page cache and the larger posting lists cost more per query. Throughput
is CPU-bound at 4 workers on the 2 vCPUs; more workers don't help.)

---

## Corpus refresh

**Clickstream** (monthly): download new `clickstream-enwiki-YYYY-MM.tsv.gz`, rebuild `click_prior.bin` and `autosuggest.json`. Triggered manually for now.

**Full corpus rebuild** (quarterly): re-run `setup.sh` from step 8 onwards. Increase `CORPUS_SIZE` at the top of `setup.sh` if you want more articles — bumping to 2M is one number change and a re-run.

**Postings format change** (e.g. when adding a new field per PRD-017): bump the zet binary, rebuild the index. The TREC doesn't need regeneration unless a new tag is being emitted.

---

## Troubleshooting

**Service won't start / crashes:**
```bash
sudo journalctl -u zettair-search -n 50
```

**`zet` crashes with `docmap_load: Assertion fd >= 0`:**
Index files are probably owned by root. Fix: `sudo chown -R zettair:zettair /mnt/wikipedia-source/`

**`git pull` fails with permission denied:**
`.git` was written by root (e.g. someone ran `sudo git pull`). Fix: `sudo chown -R deploy:deploy /opt/zettair-search`

**Snippets contain wrong content (text from a different article):**
The `>4 GB TREC` offset overflow bug. Should be fixed in zettair commit `3aba055`+. If it returns, check `git log /opt/zettair/devel/src/include/_docmap.h` for the `off_t offset` field.

**Field boost not applying:**
Check `journalctl -u zettair-search` for a `[field_boost] ZET_BOOST_TITLE = 3.00` line at startup. If missing, the env var isn't being passed through. Verify `/etc/systemd/system/zettair-search.service` has the `Environment=ZET_BOOST_TITLE=3.0` line and `systemctl daemon-reload && systemctl restart zettair-search`.

**Autosuggest returns nothing:**
```bash
curl 'http://localhost:8765/suggest?q=ei&n=5'
# if empty, rebuild: cd /opt/zettair/wikipedia && python3 build_autosuggest.py
```

**Caddy not proxying / TLS errors:**
```bash
sudo systemctl restart caddy
sudo journalctl -u caddy -n 30
```

---

## PRD index

Design decisions are recorded in `prd/`. Reading order if you're new to the codebase:

| PRD | What it covers | Status |
|-----|---------------|--------|
| PRD-006 | Click-prior ranking — clickstream data baked into BM25 | Live |
| PRD-007 | Persistent worker pool — process pool, JSON Lines protocol | Live |
| PRD-008 | Query-biased summaries — original Python summariser | Superseded by PRD-011 → PRD-016 |
| PRD-011 | C summariser — using Zettair's built-in summarise.c | Superseded by PRD-016 |
| PRD-012 | Top-1M corpus — disk budget, build pipeline | Live (now 1.5M) |
| PRD-013 | Session logging — sid, ip, result lists in query log | Draft |
| PRD-014 | Dbkey passthrough — fix Wikipedia URLs for punctuation | Superseded by PRD-015 |
| PRD-015 | Disk-resident URL store — replaces in-RAM dbkey map | Live |
| PRD-016 | Inline Python summariser — replaces C summariser | Live |
| PRD-017 | Field-weighted BM25 — title boost via per-occurrence field-id | Live |
