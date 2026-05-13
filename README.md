# Zettair Search

A full-text BM25 search engine over the top 1.5 million English Wikipedia articles, with field-weighted ranking, query-biased summaries, click-prior ranking, and autosuggest — all from real Wikipedia clickstream data.

Live at: **https://zettair.io**

---

## What it is

A production search engine built on [Zettair](https://github.com/rmit-ir/zettair), a research-grade BM25 engine from RMIT. The interesting parts are the layer on top of it:

- **Per-field BM25 (PRD-019, live)** — proper BM25F across body and title fields, each with its own length normalisation and weight (`ZET_FIELD_W_TITLE`, `ZET_FIELD_B_TITLE`, etc.). Per-doc per-field word counts are written by `zet -i` directly (sidecars `<index>.field_lengths`, `<index>.field_stats`, `<index>.docno_map.tsv` — generated in the same loop that assigns docids, so they can't drift). Generalises to up to 16 fields (4-bit field-id reserved per posting offset).
- **Knowledge panel summaries (PRD-018, M1+M2 live)** — when a query has a hand-fed summary in the `summaries.store` FlatStore (keyed by normalised query string), `/search` returns a `summary` field and the front-end renders an animated knowledge-panel card with shimmer skeleton, cascade reveal, and pulsing badge. Missing entries fall through to the existing top-result snippet, so nav queries keep their cleaner layout. M3-M6 (offline pipeline that auto-generates summaries via a local model on a Mac Mini) is still TODO.
- **Click-prior ranking** — 15 months of Wikipedia clickstream data is decay-weighted and added additively to BM25 scoring. Tuned to act as a tie-breaker (ZET_CLICK_ALPHA=0.05), not a dominator. Articles that real users actually click on rank higher when BM25 is close to even.
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
server.py          — FastAPI app: worker pool, FlatStore, summariser, search/suggest/click/queries endpoints
summarise.py       — Inline Python query-biased summariser (PRD-016)
index.html         — Single-file frontend (HTML + CSS + JS, no build step). Knowledge-panel skeleton + cascade reveal lives here (PRD-018 M2).
loadtest.py        — Load testing with latency percentiles and histogram
intent.py          — Nav-vs-info query classifier (head_floor signal); also writes summary-worthy query list (PRD-018)
digest.py          — Daily query digest (reads logs/queries.jsonl, sends Telegram summary)
requirements.txt   — Python deps: fastapi, uvicorn[standard]
tools/
  summaries_admin.py        — Manage the PRD-018 summaries FlatStore (build/add/list/get/delete)
  seed_demo_summaries.sh    — Hand-feed a few demo summaries to prod
  fetch_trending.py         — PRD-020 trending fetcher: pulls hourly pageview dumps, scores, writes current.json. PRD-021 adds the article-specificity gate.
  trending_denylist.txt     — PRD-020 user denylist (case-insensitive substring matches against title)
  build_news_summary_jobs.py — PRD-021 producer: reads current.json, enqueues :news jobs into priority/ for the Mac Mini
  compact_news_summaries.py — PRD-021 weekly compaction: drop :news entries whose subject hasn't trended recently
  enqueue.py                — manual hot-path: drop a single job into priority/ for re-run or emergency processing
deploy/
  setup.sh                  — Single entry point: idempotent, staleness-aware. Pulls repos, rebuilds zet, reindexes, etc. Holds a flock so two runs can't race.
  deploy.sh                 — CI wrapper: git pull on zettair-search, then sudo bash setup.sh
  zettair-search.service    — systemd unit for the search service
  zettair-trending.{service,timer} — PRD-020 trending fetcher, fires every 3h
tests/
  test_setup.sh             — 20-scenario harness for the setup.sh staleness logic (DRY_RUN mode)
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
              ├─▶ ZetPool — N persistent zet processes
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
`POST /click` logs `{ts, q, docno, rank, score, ip, local}` to `logs/clicks.jsonl`. Used as input for ranking experiments.

**Query log viewer (`/queries`):**
`GET /queries[?start=YYYY-MM-DD&end=YYYY-MM-DD&limit=500&include_local=0]` aggregates `logs/queries.jsonl` over a UTC date range and renders a sorted-by-count HTML table. Localhost test traffic (curl, `intent.py`, `loadtest.py` running on the box) is excluded by default; set `include_local=1` to see it. Append `&format=json` for the JSON form.

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

Configured in `deploy/zettair-search.service`. Values shown match what's deployed.

`server.py` also passes `--b=0.0` to `zet` on the worker command line, disabling BM25 length normalisation. Long canonical articles (Mark Zuckerberg, Denver) were losing per-mention-density fights against shorter related ones (Randi Zuckerberg, Denver Broncos); turning length norm off plus the increased title boost gives the canonical article room to win. PRD-019 outlines a per-field BM25 design that would let body and title have separate length-norm parameters.

| Variable | Value | Description |
|----------|-------|-------------|
| `ZET_BINARY` | `/opt/zettair/devel/zet` | Path to compiled zet binary |
| `ZET_INDEX` | `/mnt/wikipedia-source/wikiindex/index` | Path to Zettair index |
| `ZET_PORT` | `8765` (default) | HTTP port (Caddy reverse-proxies to this) |
| `ZET_WORKERS` | `4` | Persistent zet worker processes |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout in seconds |
| `ZET_CLICK_PRIOR` | `…/wikiindex/index.click_prior.bin` | Click prior float32 array (indexed by docid, lives next to the index) |
| `ZET_CLICK_ALPHA` | `0.05` | Click prior addend strength (additive, applied in `post()`). 0.5 was tried but dominated BM25; 0.05 is a tie-breaker. |
| `ZET_BOOST_TITLE` | `5.0` | Legacy per-occurrence boost for title-tagged terms (unused when PRD-019 is on) |
| `ZET_PERFIELD_BM25` | `1` | Enable PRD-019 per-field BM25 (BM25F) |
| `ZET_FIELD_W_TITLE` | `10.0` | Title field weight in BM25F |
| `ZET_FIELD_B_TITLE` | `1.0` | Title length normalisation; 1.0 = full norm so query-fills-title is a strong signal |
| `ZET_FIELD_W_BODY` | `1.0` | Body field weight in BM25F |
| `ZET_FIELD_B_BODY` | `0.0` | Body length normalisation; 0 disabled — long canonical articles were losing per-mention-density fights against short stubs |
| `ZET_SNIPPETS_STORE` | `…_snippets.store` | Pre-baked snippets (fallback for the summariser) |
| `ZET_SNIPPETS_MAP` | `…_snippets.map` | Snippets offset map |
| `ZET_IMAGES_STORE` | `…_images.store` | Wikimedia image URLs |
| `ZET_IMAGES_MAP` | `…_images.map` | Images offset map |
| `ZET_URLS_STORE` | `…_urls.store` | Canonical en.wikipedia.org URLs |
| `ZET_URLS_MAP` | `…_urls.map` | URLs offset map |
| `ZET_DOCSTORE` | `…enwiki_top1m.docstore` | Cleaned article text (read by inline summariser) |
| `ZET_DOCMAP` | `…enwiki_top1m.docmap` | Docstore offset map |
| `ZET_AUTOSUGGEST` | `…autosuggest.json` | Autosuggest sorted array |
| `ZET_SUMMARIES_STORE` | `…summaries.store` | PRD-018 knowledge-panel summaries (keyed by normalised query string) |
| `ZET_SUMMARIES_MAP` | `…summaries.map` | Summaries offset map |
| `ZET_TRENDING_CURRENT` | `…trending/current.json` | PRD-020 trending chip-rail data (read by `/api/trending`) |

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
git pull (zettair-search) → git pull (zettair) → if zettair HEAD changed, rebuild & install zet → pip install -r requirements.txt → systemctl restart → health check
```

`deploy.sh` rebuilds the zet C engine when `/opt/zettair` HEAD moves and copies `.libs/zet` over the libtool wrapper script (which would otherwise try to re-link on first invocation and fail because the running service user can't write into the build dir).

**One-time setup:**
1. `ssh-keygen -t ed25519 -f deploy_key`
2. Add `deploy_key.pub` to `~/.ssh/authorized_keys` on the VPS (as `deploy` user)
3. Add `deploy_key` as GitHub secret `VPS_SSH_KEY`
4. Add VPS IP as GitHub secret `VPS_IP`

**Reverse proxy:** Caddy is installed separately on the VPS and handles TLS termination and reverse proxying to `:8765`. It is not managed by this repo. Caddy sets `X-Forwarded-For` with the real client IP, which `server.py` reads for logging.

CI deploys rebuild the zet binary if the C source changed, but do not rebuild the index. Index changes (corpus refresh, postings format change) still need a manual `setup.sh` run on the server.

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

Baseline on CCX13 (2 vCPU, 8 GB RAM), 1.5M corpus, 4 workers, 10 concurrent clients,
PRD-019 per-field BM25 active: ~18 req/s, p50 ~475 ms, p95 ~930 ms, p99 ~1600 ms.

(Earlier numbers on the same box: 1M corpus was ~30 req/s. 1.5M with the
old single-field BM25 was ~17 req/s. PRD-019 initially regressed throughput
to ~7.5 req/s because zet was building C summaries that the Python summariser
threw away, and worker count was still 2. Dropping --summary=plain from the
zet command line and bumping ZET_WORKERS to 4 recovered throughput.)

---

## Corpus refresh

**Clickstream** (monthly): download new `clickstream-enwiki-YYYY-MM.tsv.gz`, rebuild `click_prior.bin` and `autosuggest.json`. Triggered manually for now.

**Full corpus rebuild** (quarterly): re-run `setup.sh` from step 8 onwards. Increase `CORPUS_SIZE` at the top of `setup.sh` if you want more articles — bumping to 2M is one number change and a re-run.

**Postings format change** (e.g. when adding a new field per PRD-017): bump the zet binary, rebuild the index. The TREC doesn't need regeneration unless a new tag is being emitted.

---

## Knowledge-panel summaries (PRD-018, M1+M2)

The frontend renders a Google-style knowledge panel with shimmer skeleton + cascade reveal when `/search` returns a `summary` field. The summary lives in `summaries.store` + `summaries.map` (FlatStore, same shape as snippets/images/urls; keyed by normalised query string — `lower().strip()` with collapsed inner whitespace).

**Add or update one summary on prod:**

```bash
# (run on prod, as deploy)
sudo -u zettair python3 /opt/zettair-search/tools/summaries_admin.py \
  --store /mnt/wikipedia-source/summaries.store \
  --map /mnt/wikipedia-source/summaries.map \
  add 'morrissey' '**Morrissey** is...'
sudo systemctl restart zettair-search   # server caches the offset map at startup
```

**Re-seed the demo set:**

```bash
sudo bash /opt/zettair-search/tools/seed_demo_summaries.sh
```

**List, get, delete:**

```bash
sudo -u zettair python3 /opt/zettair-search/tools/summaries_admin.py \
  --store /mnt/wikipedia-source/summaries.store \
  --map /mnt/wikipedia-source/summaries.map \
  list
```

(`get <query>` prints the markdown; `delete <query>` removes the entry from the map; `build --in foo.jsonl` does a clean rebuild from a JSONL file with `{"query": ..., "summary_md": ...}` per line.)

**Bulk pipeline (M3-M6, TODO)**: offline build_summary_jobs.py on prod → ship JSONL to Mac Mini → local model generates summaries → install_summaries.py on prod. Not built yet; for now everything's hand-fed via summaries_admin.

**Priority queue.** Alongside `pending/`, the worker also drains `/mnt/wikipedia-source/summaries/priority/` and processes it FIRST every sweep. PRD-021 news jobs land here automatically. To manually push a query through the priority lane (e.g. re-run after a prompt change, or force-add an emergency entry):

```bash
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py "elon musk"
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py --news "tristan da cunha"
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py --raw my_job.json
```

The biographical mode (no flag) builds a job from the live `/search` results; `--news` builds from the current Wikipedia article using the same heuristic as the trending fetcher; `--raw` copies a pre-built JSON file. None of them check for an existing summary — that's deliberate, since manual enqueue is for forcing re-runs.

---

## Trending pages (PRD-020)

The homepage shows a small "Trending now" / "Popular now" chip rail under the search box. Click a chip to run that search. The chips refresh every 3 hours from Wikimedia's hourly pageview dumps.

**Data flow:**

```
zettair-trending.timer (every 3h)
  → tools/fetch_trending.py
    • downloads pageviews-YYYYMMDD-HH0000.gz from dumps.wikimedia.org
    • filters denylist (Special:, year pages, List_of_*, plus tools/trending_denylist.txt)
    • normalises titles → search queries (strips parens, lowercases)
    • appends sample to /mnt/wikipedia-source/trending/history.jsonl
    • recomputes /mnt/wikipedia-source/trending/current.json with spike scores
  → server.py /api/trending (reads current.json, mtime-cached)
  → index.html homepage chip rail (loads on idle, hides if empty)
```

**Two modes, switches automatically:**

- **`raw`** — for the first ~7 days after the timer starts, before there's enough per-article history to compute a spike. Chips ordered by raw view count, denylist-filtered. Label: "Popular now".
- **`spike`** — once each article has ≥21 samples (~7 days at 3-hourly cadence), the scorer compares current views to the article's own 30-day median: `score = log((views + 100) / (median + 100))`. Articles below `log(2)` (less than 2× their baseline) drop. Label: "Trending now".

The spike score is what stops perennials like Cleopatra and Hitler — they're always-popular, not trending. Mark Carney becoming PM is trending. The data shape is also designed so a future ranking-boost feature can read `current.json` directly without recomputation.

**In-index vs external chips.** `/api/trending` joins each item's docno against our docstore. Articles we have in the corpus get an `in_index: true` chip that triggers a search; articles we don't (e.g. very fresh pages not in the 1.5M cut) get an `in_index: false` chip with a different icon that links directly to en.wikipedia.org. This avoids the failure mode where a trending article routes to an empty results page.

**Article-specificity gate (PRD-021).** After the pageview-shape filters, each candidate's Wikipedia article is fetched via the REST API and scored for "recent dated event" paragraphs (day-precision dates within the last 14 days). Articles without a qualifying paragraph drop off the rail entirely — this is the same signal that decides whether we have content for a news-flavoured knowledge-panel summary, and it doubles as a strong noise filter (community pile-ons on Wikipedia articles editors haven't touched fall off automatically). `event_paragraph` + `event_date` are persisted into `current.json` so downstream consumers don't need a second API fetch.

**Trending feeds the next corpus rebuild.** `select_top_articles.py` reads `/mnt/wikipedia-source/trending/history.jsonl` and union-s every title that has ever appeared in a sample on top of the top-N clickstream cut. So next time the index is rebuilt (new enwiki dump, fresh TREC), every article that's been popular since the last rebuild is in the corpus by construction. The corpus grows by however many trending-only titles have accumulated (typically tens of thousands over a 30-day window); the README still loosely says "1.5M" but the actual number creeps up after each rebuild. `setup.sh` only regenerates `top_titles.txt` when it's missing — to force a refresh that picks up new trending titles, `sudo rm /mnt/wikipedia-source/top_titles.txt` and re-run setup.sh. (Auto-trigger on every CI deploy was tried briefly but made deploys take 20+ min reading the monthly clickstream files; not worth it for a 4-8h downstream rebuild that's gated separately.)

**Manual ops:**

```bash
# Force a fetch outside the timer
sudo -u zettair python3 /opt/zettair-search/tools/fetch_trending.py

# Bootstrap 7 days of history in one shot (skips matched hours).
# Use this on a fresh install to jump-start spike-mode scoring
# instead of waiting a week for the live timer to accumulate samples.
# ~56 dumps, ~2.8 GB downloaded, 5-10 min.
sudo -u zettair python3 /opt/zettair-search/tools/fetch_trending.py --bootstrap 7

# Trim history.jsonl to last 30 days
sudo -u zettair python3 /opt/zettair-search/tools/fetch_trending.py --compact

# See what's currently on the rail
curl -s https://zettair.io/api/trending | python3 -m json.tool

# Logs
sudo tail -n 50 /mnt/wikipedia-source/trending/fetch.log
sudo journalctl -u zettair-trending.service -n 30
```

**Editing the denylist** (region-specific perennials, adult content, etc.):

`tools/trending_denylist.txt` is plain text — one substring per line, case-insensitive partial match against the lowercased title (underscores → spaces). Structural junk (year articles, Special:, List_of_*) is regex-handled in `fetch_trending.py` and doesn't need to be in this file.

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
The `[field_boost] ZET_BOOST_TITLE = 5.00` line is logged to zet's stderr, which `server.py` captures via `asyncio.subprocess.PIPE` and does not forward — so it won't show in journald. To verify the boost is loading, run zet manually with the same env: `sudo -u zettair env ZET_BOOST_TITLE=5.0 /opt/zettair/devel/zet -f /mnt/wikipedia-source/wikiindex/index --okapi --b=0.0 -n 3 <<< 'test' 2>&1 | head`. If the field_boost line is missing, check `/etc/systemd/system/zettair-search.service` and `systemctl daemon-reload && systemctl restart zettair-search`.

**Click prior loaded but not affecting scores:**
After a corpus refresh, rebuild `click_prior.bin` against the *current* index — the file is keyed by Zettair internal docid, which shifts on every reindex. Run `cd /opt/zettair/wikipedia && python3 build_docno_map.py /mnt/wikipedia-source/enwiki_top1m.trec && python3 build_click_prior.py && cp click_prior.bin /mnt/wikipedia-source/click_prior.bin && sudo systemctl restart zettair-search`. Verify by running zet manually — top-20 click scores should look like a list of obvious popular articles (Elon_Musk, Donald_Trump, Main_Page, etc.).

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
| PRD-018 | Knowledge panel — offline-generated query summaries | M1+M2 live (server plumbing + frontend animations). M3-M6 (offline pipeline) TODO. |
| PRD-019 | Per-field BM25 (BM25F) — separate length norm and weight per field, generalises to N fields | Live (M1+M2+M3 on prod). Folding sidecars into docmap is the only remaining TODO. |
| PRD-020 | Trending pages — spiking signal from Wikipedia pageview dumps, homepage chip rail | M1-M3 in (fetcher + endpoint + chip rail). M4 spike-scoring activates automatically after ~7 days of samples. M6 (ranking boost) and M7 (Zeitgeist page) deferred to future PRDs. |
| PRD-021 | News-spike summaries — when a query is trending, knowledge panel explains why via a Wikipedia-grounded news summary. Article-specificity gate doubles as the trending rail's quality filter. | M1-M4 in (specificity gate + news producer + server serving + weekly compaction). |
| PRD-022 | News-headline fallback — when Wikipedia has no recent event paragraph, fetch Google News RSS for the query and synthesise an event_paragraph from the top headlines. Same downstream pipeline as Wikipedia-sourced. | M1+M2 in. |
| PRD-023 | Feature ideas backlog — long-form notes on differentiating features we could build (ask-a-question, compare view, timeline, cite-this, image grid, audio mode, etc.). Idea backlog, not a build plan. | Drafted. |
| PRD-024 | Cite this — one-click APA / MLA / Chicago / Harvard / BibTeX citation popover on every result and the knowledge panel. Pure frontend. | M1-M2 live. |
| PRD-025 | Related entities — random-walk graph over Wikipedia entity articles, surfaced as a right-rail panel of related entities on the results page. All offline batch on prod. | Drafted. |
