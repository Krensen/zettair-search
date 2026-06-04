# Zettair Search

A full-text BM25 search engine over the top ~1.5 million English Wikipedia articles, with per-field BM25F ranking, query-biased + offline-generated summaries, click-prior ranking, autosuggest, a trending-news rail, related-entity panel, reading-time + difficulty signal, and a one-click citation popover — all from real Wikipedia clickstream data.

Live at: **https://zettair.io**

---

## Why this README is verbose

This file is the **single source of truth** for someone (or some AI) coming back to the codebase cold. It captures things that aren't derivable from the code: operational quirks, past incidents, why specific knobs are set the way they are. Treat each "Operational notes" / "Incident log" entry as load-bearing — most of them encode something we got wrong once.

---

## What it is

A production search engine built on [Zettair](https://github.com/rmit-ir/zettair), a research-grade BM25 engine from RMIT. The interesting parts are the layer on top of it:

- **Per-field BM25 (PRD-019, live)** — proper BM25F across body and title fields, each with its own length normalisation and weight (`ZET_FIELD_W_TITLE`, `ZET_FIELD_B_TITLE`, etc.). Per-doc per-field word counts are written by `zet -i` directly (sidecars `<index>.field_lengths`, `<index>.field_stats`, `<index>.docno_map.tsv` — generated in the same loop that assigns docids, so they can't drift). Generalises to up to 16 fields (4-bit field-id reserved per posting offset).
- **Knowledge panel summaries (PRD-018)** — when a query has a summary in `summaries.store` (FlatStore keyed by normalised query string), `/search` returns a `summary` field and the front-end renders an animated panel with shimmer skeleton + cascade reveal. The offline pipeline (M3-M6) is now operational: a Mac Mini (`sparky` user, separate machine) drains `priority/` then `pending/` job dirs, ships `.md` files back, the prod installer drops them into the FlatStore and bounces the service. News (`<query>:news`) jobs always go to `priority/`.
- **News-spike summaries (PRD-021/022/026)** — when a query is currently trending AND the article has a "recent dated event" paragraph (or, fallback, Google News headlines from the past 7 days), the knowledge panel shows a news-flavoured summary instead. The article-specificity gate doubles as the trending rail's quality filter. PRD-026 split the rail sources into spike-pipeline + Google News top stories + Wikipedia "In the news" portal, with source-weighted sorting (Google → spike → ITN).
- **Related entities (PRD-025)** — random-walk graph over Wikipedia entity articles, same-class-only at output. Right-rail panel of related entities on the results page. Offline batch: `build_entity_set.py` → `build_link_graph.py` → `build_related.py`, all in the `zettair/wikipedia/` repo. Server reads `related.store` + `related.map` at startup.
- **Reading time + difficulty (PRD-027)** — every result card and the knowledge panel show a single neutral-grey "corner pill" with a Harvey-ball difficulty dot (¼ accessible / ½ moderate / ¾ technical / ● dense / ○ unknown) and a reading-time number. Computed offline from the docstore by `tools/build_reading_sidecar.py`; sidecar is `enwiki_top1m.reading.bin` (RDT2 magic, ~9 MB packed). Format-magic check in setup.sh so a sidecar from an older format gets rebuilt automatically.
- **Cite this (PRD-024)** — quiet `cite` text-link trailing each result snippet (plus on the knowledge panel). Click opens a popover with APA / MLA / Chicago / Harvard / BibTeX strings, each with a copy button. Pure frontend.
- **Click-prior ranking (PRD-006)** — 15 months of Wikipedia clickstream data, decay-weighted, added additively to BM25 scoring as a tie-breaker (`ZET_CLICK_ALPHA=0.05`).
- **Query-biased summaries (PRD-008/011/016)** — Python summariser ported from the Turpin/Hawking/Williams SIGIR 2003 algorithm, called inline by the FastAPI server. Reads cleaned article text from the disk-resident docstore. Fallback to pre-baked `_snippets_store`.
- **Autosuggest** — ~1M queries ranked by clickstream popularity, served via binary search in ~1 ms.
- **Persistent worker pool (PRD-007)** — 4 long-lived `zet` processes with the index memory-mapped. Queries arrive via stdin, results come back as JSON Lines. ~50× lower latency than spawning a process per query.
- **Disk-based sidecar stores** — snippets, images, URLs, document text, summaries, related, reading sidecar — all flat binary files with a JSON offset map. `os.pread()` seeks to the exact byte range per result; only the offset maps stay in RAM.
- **Periodic news-rail refresh** — homepage rail refetches `/api/trending` every 15–25 min (jittered per client so cohorts don't realign), only while tab is visible. Refresh on visibility-change when older than 10 min.

---

## Two repos

| Repo | What's in it |
|------|-------------|
| [`Krensen/zettair`](https://github.com/Krensen/zettair) | Patched Zettair C source, Wikipedia pipeline scripts including the offline image builder, entity-class extractor, link-graph + related-walk builders |
| [`Krensen/zettair-search`](https://github.com/Krensen/zettair-search) | FastAPI server, frontend (`index.html`), tools, deploy scripts, PRDs |

The C patches in `zettair`: ARM64 build support; click-prior scoring (`okapi.c`); 64-bit byte offsets in the docmap; 4-bit field-id in each posting offset (PRD-017); all non-Okapi rankers removed.

The pipeline scripts in `zettair/wikipedia/` produce most of the data files the server needs at startup. The reading-time sidecar is the exception — it's produced by `tools/build_reading_sidecar.py` in *this* repo, because we want to iterate on FK thresholds without rebuilding the corpus.

There is **also a third repo** (`Krensen/zettair-ios`, not yet open-sourced) that contains the iOS app under PRD-028. See `prd/PRD-028-ios-app.md` for the API-stability and coordination notes — the bottom of that file is a running handoff log for asks between the iOS effort and this repo.

---

## Repository layout (`zettair-search`)

```
server.py             — FastAPI app: worker pool, FlatStore, summariser, /search /suggest /click /queries /api/trending /img /article endpoints
summarise.py          — Inline Python query-biased summariser (PRD-016)
index.html            — Single-file frontend (HTML + CSS + JS, no build step). Knowledge panel, related rail, trending rail with staleness fade, corner-pill, cite popover all live here.
loadtest.py           — Load testing with latency percentiles and histogram
intent.py             — Nav-vs-info query classifier (head_floor signal); also writes summary-worthy query list (PRD-018)
digest.py             — Daily query digest (Telegram)
requirements.txt      — Python deps: fastapi, uvicorn[standard]
tools/
  build_reading_sidecar.py   — PRD-027 reading-time + difficulty sidecar builder (RDT2 magic)
  summaries_admin.py         — Manage the summaries FlatStore (build/add/list/get/delete)
  seed_demo_summaries.sh     — Hand-feed a few demo summaries to prod
  fetch_trending.py          — PRD-020 trending fetcher: pulls hourly pageview dumps, scores, writes current.json. Specificity gate + Google News + Wikipedia ITN per PRD-026.
  trending_denylist.txt      — PRD-020 user denylist
  build_summary_jobs.py      — PRD-018 bulk biographical producer (pending/)
  build_news_summary_jobs.py — PRD-021 news producer (priority/)
  install_summaries.py       — PRD-018 installer (drains done/, writes FlatStore, bounces service)
  compact_news_summaries.py  — PRD-021 weekly compaction: drop :news entries whose subject hasn't trended recently
  enqueue.py                 — manual hot-path: drop a single job into priority/ for re-run or emergency
deploy/
  setup.sh                                  — Single entry point: idempotent, staleness-aware. Holds a flock.
  deploy.sh                                 — CI wrapper: git pull on zettair-search, then sudo bash setup.sh
  zettair-search.service                    — systemd unit for the search service
  zettair-trending.{service,timer}          — PRD-020 trending fetcher, hourly. MemoryMax=1G.
  zettair-trending-compact.{service,timer}  — Weekly history.jsonl compaction
  zettair-news-summary-producer.{service,timer}  — PRD-021 news producer, every 30 min (also chained ExecStartPost of trending fetcher)
  zettair-summary-producer.{service,timer}  — PRD-018 bulk biographical producer
  zettair-summary-installer.{service,timer} — PRD-018 installer, every 5 min
  zettair-news-compact.{service,timer}      — PRD-021 weekly :news compaction
tests/
  test_setup.sh             — 20-scenario harness for the setup.sh staleness logic (DRY_RUN mode)
.github/workflows/deploy.yml — GitHub Actions: SSH → deploy.sh on push to main
prd/                  — Product requirements documents for each major feature
mockups/              — Static HTML mockups for UI iteration (e.g. result-meta layout variants)
logs/                 — queries.jsonl, clicks.jsonl, zet_crashes.jsonl (gitignored)
```

---

## How the server works

```
Browser (HTTPS)
  └─▶ Caddy (VPS, handles TLS + reverse proxy, no Cloudflare Tunnel)
        └─▶ server.py :8765 (FastAPI / uvicorn)
              ├─▶ ZetPool — N persistent zet processes
              │     query via stdin → JSON Lines on stdout
              │     index is memory-mapped by the OS
              │     PRD-019 BM25F applied at score time
              ├─▶ FlatStore (_docstore)        — os.pread() for the inline summariser
              ├─▶ FlatStore (_snippets_store)  — pre-baked snippet fallback
              ├─▶ FlatStore (_images_store)    — Wikimedia image URLs (300px-, historical — see /img rewrite below)
              ├─▶ FlatStore (_urls_store)      — canonical en.wikipedia.org URLs
              ├─▶ FlatStore (_summaries_store) — PRD-018 knowledge-panel summaries (keyed by query_norm; `<qn>:news` for news variants)
              ├─▶ FlatStore (_related_store)   — PRD-025 related-entity lists
              ├─▶ _related_class               — PRD-025 entity class labels (dict)
              ├─▶ _reading_time + _difficulty  — PRD-027 in-RAM dicts loaded from RDT2 sidecar
              └─▶ _autosuggest list            — sorted (query, count) pairs
```

**Query flow:**
1. `GET /search?q=einstein&n=10` arrives at `server.py`
2. A semaphore acquires one of the 4 `zet` workers
3. The query is written to the worker's stdin; JSON Lines are read from stdout
4. Each result line has `rank`, `docno`, `score`, `docid`
5. `enrich_results()` per result:
   - reads the article text from `_docstore` and runs `summarise.summarise_doc()` (falls back to `_snippets_store` on miss)
   - looks up the canonical URL in `_urls_store` (or builds it from the docno)
   - looks up the image URL in `_images_store`
   - looks up `reading_time_min` and `difficulty` in the PRD-027 dicts
6. PRD-018: looks up `summary` in `_summaries_store` (prefers `<query_norm>:news` when the query is spiking)
7. PRD-025: for the top result, looks up `related` in `_related_store` (same-class only, capped at 8)
8. Response returned; worker released back to the pool.

**Click flow:**
`POST /click` logs `{ts, q, docno, rank, score, ip, local}` to `logs/clicks.jsonl`. Used as input for ranking experiments.

**Query log viewer (`/queries`):**
`GET /queries[?start=YYYY-MM-DD&end=YYYY-MM-DD&limit=500&include_local=0]` aggregates `logs/queries.jsonl` over a UTC date range. Localhost test traffic excluded by default; append `&format=json` for JSON.

**Image proxy (`/img`):**
`GET /img?url=<wikimedia URL>` proxies a Wikimedia thumbnail. Implements a Wikimedia-thumb-size allowlist enforcer (Wikimedia tightened its CDN in May 2026; see Incident log). Upstream `HTTPError` codes pass through; `URLError` becomes 502.

---

## Data files (on `/mnt/wikipedia-source/`)

All large files live on a separate Hetzner volume, not the boot disk.

| File | Size | What it is |
|------|------|------------|
| `enwiki_top1m.trec` | ~30 GB | 1.5M Wikipedia articles in TREC format with `<TITLE>` tags |
| `wikiindex/` | ~12 GB | Zettair inverted index (4-bit field-id per offset, may split across `index.v.0/v.1` if >4 GB) |
| `wikiindex/index.click_prior.bin` | ~6 MB | float32 array indexed by Zettair docid |
| `enwiki_top1m_snippets.{store,map}` | ~540 MB + ~70 MB | Pre-baked snippet text |
| `enwiki_top1m_images.{store,map}` | ~120 MB + ~30 MB | Wikimedia image URLs (built with `300px-` URLs — proxy rewrites at fetch time) |
| `enwiki_top1m_urls.{store,map}` | ~18 MB + ~14 MB | Canonical URLs for the ~330k articles whose dbkey ≠ safe_id |
| `enwiki_top1m_titles.{store,map}` | ~50 MB + ~70 MB | PRD-031 canonical Wikipedia display title per docno (100% coverage); replaces the URL-parsing hack for title rendering |
| `enwiki_top1m.docstore` + `.docmap` | ~13 GB + ~57 MB | Cleaned article text for the inline summariser |
| `enwiki_top1m.reading.bin` | ~9 MB | PRD-027 reading-time + difficulty sidecar (RDT2 packed binary) |
| `autosuggest.json` | ~27 MB | Sorted `[[query, count], ...]` |
| `summaries.{store,map}` | varies | PRD-018 knowledge-panel summaries (biographical + `<qn>:news`) |
| `related.{store,map}` | ~390 MB + ~25 MB | PRD-025 per-entity ranked related lists |
| `related/entity_class.json` | ~30 MB | PRD-025 docno → class label (human/place/org/work/event) |
| `related/entity_titles.tsv` | varies | PRD-025 named-entity titles (also consumed by PRD-026) |
| `trending/current.json` | small | PRD-020 chip rail data; mtime-cached by server |
| `trending/history.jsonl` | grows ~50 MB/mo | PRD-020 sampled top-N per hour, compacted weekly |
| `summaries/{priority,pending,done,errors,installed}/` | varies | PRD-018 job queue dirs |

The filename prefix `enwiki_top1m` is historical; the corpus is now ≥1.5M and creeps up after every rebuild (see "Trending feeds the next corpus rebuild" below).

---

## Environment variables

Configured in `deploy/zettair-search.service`. Values shown match what's deployed.

`server.py` also passes `--b=0.0` to `zet` on the worker command line, disabling per-field length normalisation on the body. Long canonical articles were losing per-mention-density fights against shorter related ones; turning length norm off + a higher title weight gives the canonical article room to win.

| Variable | Value | Description |
|----------|-------|-------------|
| `ZET_BINARY` | `/opt/zettair/devel/zet` | Compiled zet binary |
| `ZET_INDEX` | `/mnt/wikipedia-source/wikiindex/index` | Zettair index prefix |
| `ZET_PORT` | `8765` | HTTP port (Caddy reverse-proxies to this) |
| `ZET_WORKERS` | `4` | Persistent zet worker processes |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout in seconds |
| `ZET_CLICK_PRIOR` | `…/wikiindex/index.click_prior.bin` | Click prior float32 array (indexed by docid, lives next to the index) |
| `ZET_CLICK_ALPHA` | `0.05` | Click prior addend strength (additive). 0.5 dominated BM25; 0.05 is a tie-breaker. |
| `ZET_BOOST_TITLE` | `5.0` | Legacy per-occurrence boost (unused when PRD-019 is on) |
| `ZET_PERFIELD_BM25` | `1` | Enable PRD-019 BM25F |
| `ZET_FIELD_W_TITLE` | `10.0` | Title field weight |
| `ZET_FIELD_B_TITLE` | `1.0` | Title length normalisation (1.0 = full norm) |
| `ZET_FIELD_W_BODY` | `1.0` | Body field weight |
| `ZET_FIELD_B_BODY` | `0.0` | Body length normalisation disabled |
| `ZET_SNIPPETS_STORE/MAP` | `…_snippets.store/.map` | Pre-baked snippets |
| `ZET_IMAGES_STORE/MAP` | `…_images.store/.map` | Wikimedia image URLs |
| `ZET_URLS_STORE/MAP` | `…_urls.store/.map` | Canonical en.wikipedia.org URLs |
| `ZET_TITLES_STORE/MAP` | `…_titles.store/.map` | PRD-031 canonical display titles per docno |
| `ZET_DOCSTORE` | `…enwiki_top1m.docstore` | Cleaned article text |
| `ZET_DOCMAP` | `…enwiki_top1m.docmap` | Docstore offset map |
| `ZET_AUTOSUGGEST` | `…autosuggest.json` | Autosuggest sorted array |
| `ZET_SUMMARIES_STORE/MAP` | `…summaries.store/.map` | PRD-018 summaries |
| `ZET_TRENDING_CURRENT` | `…trending/current.json` | PRD-020 trending chip-rail data |
| `ZET_RELATED_STORE/MAP` | `…related.store/.map` | PRD-025 related-entity lists |
| `ZET_RELATED_CLASS` | `…related/entity_class.json` | PRD-025 entity class labels |
| `ZET_READING_SIDECAR` | `…enwiki_top1m.reading.bin` | PRD-027 reading-time + difficulty sidecar (must be set; the default falls back to local-dev path and the file silently misses on prod) |

---

## Fresh VPS setup

```bash
sudo bash deploy/setup.sh
```

`setup.sh` runs all steps in dependency order, guarded by mtime-based staleness checks so it's safe to re-run after any failure. Holds a `flock` so two runs cannot race. **A 20-scenario test harness (`tests/test_setup.sh`) covers the staleness logic** — run before changing setup.sh.

Key steps, in order:

1. Install system packages and Python deps (one-time, gated on `/etc/zettair-setup-done`)
2. Create `deploy` and `zettair` users
3. Verify the volume is mounted at `/mnt/wikipedia-source/`
4. Clone both repos into `/opt/`
5. Build the zet binary (ARM detection; copies `.libs/zet` over the libtool wrapper)
6. Download the enwiki bz2 dump (~24 GB)
7. Download 15 months of clickstream data (~7.4 GB) into `/opt/zettair/wikipedia/`
8. Run `select_top_articles.py` → `top_titles.txt` (top 1.5M by decayed click score + trending union)
9. Run `wiki2trec.py` (~6 hours streaming the bz2)
10. (Optional) Auto-delete bz2 if free space drops AND `BZ2_AUTO_DELETE=1` is set — **off by default** because deleting the bz2 broke PRD-025/026 rebuilds twice
11. Build docno map, click prior, autosuggest, docstore, URL store
12. **§14a — PRD-027 reading-time + difficulty sidecar** — rebuilds when docstore is newer OR the magic bytes don't match `RDT2`
13. **§14b — PRD-025 entity-classes + link-graph + related-store** — multi-hour first-run, gated on bz2 presence (graceful skip if bz2 was deleted)
14. Install systemd units and timers (rsynced every deploy)
15. Restart `zettair-search`; health check
16. Verify ownership; report any root-owned files

(Caddy reverse proxy is installed separately — not managed by this script.)

**Prerequisites:**
- Ubuntu 24.04 ARM64 or x86_64
- Hetzner CCX13 (2 vCPU, 8 GB RAM) or equivalent
- 80 GB attached volume mounted at `/mnt/wikipedia-source/`
- ~8–12 hours total wall time, mostly the wiki2trec.py bz2 stream and PRD-025's first-run build

Config at top of `setup.sh`: `CORPUS_SIZE`, `CLICKSTREAM_MONTHS`, `DEPLOY_USER`, `SERVICE_USER`, `VOLUME`, `ENWIKI_DUMP_URL`, `BZ2_AUTO_DELETE`, `BZ2_DELETE_THRESHOLD_GB`.

---

## CI/CD

Every push to `main` triggers `.github/workflows/deploy.yml`, which SSHs to the VPS and runs `deploy/deploy.sh`:

```
git pull (zettair-search) → git pull (zettair) → if zettair HEAD changed, rebuild & install zet → pip install -r requirements.txt → setup.sh (staleness checks) → systemctl restart → health check
```

CI deploys rebuild the zet binary if the C source changed, and re-rsync any timer/service unit files. Index changes (corpus refresh, postings format change) still need a manual `setup.sh` run with the appropriate input files refreshed first.

**One-time setup:** `ssh-keygen -t ed25519 -f deploy_key`; add `deploy_key.pub` to `~/.ssh/authorized_keys` on the VPS as `deploy`; add `deploy_key` and the VPS IP as GitHub secrets `VPS_SSH_KEY` / `VPS_IP`.

**Reverse proxy:** Caddy on the VPS handles TLS + reverse proxy to `:8765`. Not managed by this repo. Caddy sets `X-Forwarded-For` with the real client IP; `server.py` trusts it only when the peer is loopback.

---

## Users and permissions

| User | Purpose | Owns |
|------|---------|------|
| `deploy` | Git pulls, deploys, admin | `/opt/zettair-search`, `/opt/zettair` |
| `zettair` | Runs the search service | `/mnt/wikipedia-source/` (mode 750) |
| `sparky` | Mac Mini summariser (remote user, ssh in via rsync only) | Writes `done/` + `errors/`; cannot touch `installed/` |

Both repos are world-readable (`chmod o+rX`) so the `zettair` service user can read `server.py` and the `zet` binary without owning them. The `summariser` group spans `zettair` and `sparky` so the queue dirs are group-writable.

---

## Timers (systemd) and their cadence

| Timer | Cadence | What it does |
|-------|---------|--------------|
| `zettair-trending.timer` | **every 1 h** | Pulls hourly pageview dump, scores, writes `current.json`. **`ExecStartPost` chains the news-summary producer** so a fresh sample immediately enqueues new :news jobs without waiting for the producer's own timer. |
| `zettair-news-summary-producer.timer` | **every 30 min** | Idempotent. Catch-up backstop in case the chained run in zettair-trending fails. |
| `zettair-summary-producer.timer` | per file | PRD-018 bulk biographical producer |
| `zettair-summary-installer.timer` | **every 5 min** | Drains `summaries/done/*.md` into the FlatStore, restarts `zettair-search` if anything landed |
| `zettair-trending-compact.timer` | weekly | Drops `history.jsonl` samples older than 30d |
| `zettair-news-compact.timer` | weekly | PRD-021: drops `:news` summaries whose subject hasn't trended recently |

These cadences are deliberately faster than the original PRD-020/021 defaults (was 3h / 3h). See Incident log: "no news for any spike" was caused by the 3h producer cadence + a Mac Mini-side throughput issue. **Both producer + trending are now idempotent on no-op runs**, so the faster cadence is essentially free.

---

## Operational notes (things to know, not in the code)

- **Deployment topology**: Caddy is the public-facing reverse proxy. No Cloudflare Tunnel. Trust `X-Forwarded-For` only when peer is loopback.
- **Prod one-liners**: Single lines only when pasting to chat — even single lines can mangle on wrap. If a command has metacharacters (heredocs, embedded quotes, apostrophes in commit messages), commit it to the repo and have the user pull.
- **`setup.sh` staleness caution**: New staleness triggers must be cheap (mtime / magic-byte read) and must have a downstream that actually consumes them. Auto-triggers that have no consumer slow every deploy without benefit.
- **`bz2` is kept by default**: setup.sh used to auto-delete the enwiki bz2 to save space, which broke PRD-025/026 rebuilds twice. The behaviour is now opt-in via `BZ2_AUTO_DELETE=1`.
- **PRD-027 sidecar magic**: bump the magic when format changes. The current is `RDT2` (codes 0/1/2/3/4 = unknown/accessible/moderate/technical/dense). setup.sh re-reads the magic each deploy and rebuilds if it doesn't match `READING_MAGIC_EXPECTED`. Server logs `WARNING: ... bad magic ...` on load mismatch.
- **Wikimedia thumb allowlist**: only 20, 40, 60, 120, 250, 330, 500, 960, 1280, 1920, 3840 are accepted as widths. Our `_images_store` was built with `300px-` — the `/img` proxy rewrites non-allowed widths to the smallest allowed ≥ requested (300 → 330). Allowed widths pass through verbatim. Rewrite logic in `_rewrite_thumb_size` in server.py.
- **UI is plain `index.html`** — no build step. Look for inline JS at the bottom; CSS at the top. Result-card layout: title row, then a single corner-pill in the top-right with Harvey-ball + reading-time, then snippet with an inline `cite` text-link at the end.
- **`object-position: center top`** on result + KP thumbnails — Wikipedia infobox portraits frame the subject head near the top of the image, so default-centred crop chopped heads off.
- **The iOS app exists** (PRD-028). Server changes must keep the documented `/search`, `/suggest`, `/api/trending`, `/api/related`, `/click` shapes stable. New fields are fine; renaming or removing existing fields breaks the app silently. PRD-028 includes a "server-side handoff log" with open + closed asks.

---

## Incident log

Append-only. Each entry: date, symptom, root cause, fix.

- **2026-05-25 — pageview dumps stalled ~62 h (Wikimedia)**. Wikimedia stopped publishing hourly pageview dumps for ~62 h. Our trending pipeline cleanly logged "no dump available in the last 12h — exiting cleanly"; nothing to fix on our side. Lookback widened from 12 h to 96 h so that post-outage runs auto-catch-up. UI added a stale-rail fade (`opacity 0.55`) and "(N h ago)" label when `generated_at > 24 h`.
- **2026-05-24 — `/img` returned same bytes regardless of width (iOS)**. My 2026-05-22 fix for the Wikimedia thumb allowlist rewrote *every* `/{N}px-/` to `/250px-/`. Now: allowed widths pass through; only non-allowed coerce to the smallest allowed ≥ requested. Also added `image_url` to `/api/trending` so iOS doesn't need N parallel `/search?n=1` calls per home view.
- **2026-05-22 — Wikimedia tightened thumb-size allowlist**. Existing image-store URLs (`/300px-/`) started returning HTTP 400. Introduced `_rewrite_thumb_size` in the proxy.
- **2026-05-21 — `zettair-trending.service` OOM-killed every run**. `MemoryMax=512M` was tight against PRD-022/026's expanded gate (which fetches Wikipedia wikitext + Google News headlines for up to 150 candidates) and the 1.7M-entry counts dict. Streaming-decompressed the gzipped dump (no full-body buffer); raised `MemoryMax` to `1G`. Box has 7.6 GiB RAM; service uses ~300-500 MB steady-state.
- **2026-05-19 — first PRD-027 build hung silently for 48 min**. Python `print` to non-tty SSH stdout buffers. Added `flush=True` and `python3 -u`; lowered `progress_every` to 10k so heartbeat fires every ~20–30 s.
- **2026-05-19 — PRD-027 sidecar served all `None`**. Loader fell back to a local-dev default path. Added `ZET_READING_SIDECAR` env var in the systemd unit; on prod it must point at `/mnt/wikipedia-source/enwiki_top1m.reading.bin`.
- **2026-05-17 — news-rail "all spike chips have no news"**. Producer was 3 h cadence; Mac Mini drain was slow. Producer bumped to 30 min, trending to 1 h. Both idempotent. Underlying Mac Mini-side throughput is in the `zettair-ios` adjacent repo — out of scope here.
- **2026-05-13 — PRD-026 deploy failed on `build_entity_set.py`**. Pre-fix: setup.sh was deleting the bz2 after TREC was built; PRD-025 wanted to re-read it for entity classification. Pre-fix: bz2 was also mid-download. Fix: keep bz2 by default (opt-in delete via `BZ2_AUTO_DELETE=1`); graceful skip if bz2 absent.
- **Earlier — `>4 GB TREC offset overflow`**. Snippet bodies pointed at wrong articles past 4 GB. Fixed in zettair commit `3aba055`+ (64-bit offsets in the docmap).

---

## Load testing

```bash
python3 loadtest.py --duration 600 --workers 10
python3 loadtest.py --url https://zettair.io --duration 120 --workers 4
```

Fetches ~38k real queries from `/suggest` (weighted by click count), fires them concurrently, reports mean/p50/p75/p90/p95/p99/max and a latency histogram.

Baseline on CCX13 (2 vCPU, 8 GB RAM), 1.5M corpus, 4 workers, 10 concurrent clients, PRD-019 active: ~18 req/s, p50 ~475 ms, p95 ~930 ms, p99 ~1600 ms. (PRD-019 initially regressed throughput to ~7.5 req/s because zet was building C summaries that the Python summariser threw away; dropping `--summary=plain` and bumping `ZET_WORKERS` to 4 recovered it.)

---

## Corpus refresh

**Clickstream** (monthly): drop new `clickstream-enwiki-YYYY-MM.tsv.gz` into `/opt/zettair/wikipedia/`. `setup.sh` rebuilds `click_prior.bin` and `autosuggest.json` automatically (mtime check).

**Full corpus rebuild** (quarterly): re-download enwiki bz2, `sudo rm /mnt/wikipedia-source/top_titles.txt` to force a fresh top-titles pick (which unions clickstream + trending history), re-run `setup.sh`. Bump `CORPUS_SIZE` in `setup.sh` for more articles.

**Postings format change** (new field per PRD-017): bump the zet binary; rebuild the index. TREC doesn't need regeneration unless a new tag is being emitted.

**Trending feeds the next corpus rebuild.** `select_top_articles.py` reads `trending/history.jsonl` and union-s every title that ever appeared in a sample on top of the top-N clickstream cut. So next rebuild includes every trending article since the last rebuild. To force this: `sudo rm /mnt/wikipedia-source/top_titles.txt` and re-run setup.sh. (Auto-trigger on every CI deploy was tried but made deploys take 20+ min — not worth it for a 4-8h downstream rebuild that's gated separately.)

---

## Knowledge-panel summaries (PRD-018 + PRD-021/022)

Two flavours of summary, same FlatStore (`summaries.store`):

- **Biographical** — key = `query_norm` (e.g. `"morrissey"`). Default; rendered with "Summary" badge.
- **News** — key = `<query_norm>:news` (e.g. `"barack obama:news"`). Used when the query is currently spiking AND the news summary exists. Rendered with "In the news" badge.

**Add/update by hand:**

```bash
sudo -u zettair python3 /opt/zettair-search/tools/summaries_admin.py \
  --store /mnt/wikipedia-source/summaries.store \
  --map   /mnt/wikipedia-source/summaries.map \
  add 'morrissey' '**Morrissey** is...'
sudo systemctl restart zettair-search
```

**Force-enqueue a job** (skips dedupe checks):

```bash
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py "elon musk"               # biographical
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py --news "tristan da cunha" # news
sudo -u zettair python3 /opt/zettair-search/tools/enqueue.py --raw my_job.json         # pre-built
```

**Queue lanes**: `priority/` drained first (news jobs land here), then `pending/` (bulk biographical). Workers move done jobs to `done/`; the prod installer drains every 5 min.

---

## Trending pages (PRD-020 / PRD-021 / PRD-022 / PRD-026)

The homepage shows a small "News" / "Popular" chip rail under the search box. Click a chip to run that search.

**Data flow:**

```
zettair-trending.timer (every 1 h)
  → tools/fetch_trending.py
    • downloads pageviews-YYYYMMDD-HH0000.gz from dumps.wikimedia.org (96h lookback, skips hours we have)
    • filters denylist (Special:, year pages, List_of_*, trending_denylist.txt)
    • normalises titles → search queries
    • appends sample to history.jsonl, recomputes current.json with spike scores
    • PRD-026: also fetches Google News top stories + Wikipedia ITN portal,
      unions into the candidate set
    • PRD-021/022: specificity gate fetches each candidate's Wikipedia article
      and looks for a recent dated event paragraph; falls back to Google News
      RSS headlines for the query
    • source-weighted final sort: google_news → spike → wiki_itn, capped at RAIL_MAX
  → ExecStartPost runs tools/build_news_summary_jobs.py
    • for each item with an event_paragraph, enqueues `<query_norm>:news.json` into priority/
  → server.py /api/trending (reads current.json, mtime-cached)
  → index.html homepage chip rail (loads on idle, refetches every 15-25 min jittered,
    fades + appends "(N h ago)" when current.json is >24 h stale)
```

**Two modes, switches automatically:**

- **`raw`** — first ~7 days. Chips ordered by raw view count. Label: "Popular".
- **`spike`** — once each article has ≥21 samples (~7d). `score = log((views + 100) / (median + 100))`. Drops articles below `log(2)`. Label: "News".

**Article-specificity gate (PRD-021/022)**: each candidate must have either a recent (last 14 d) dated event paragraph in its Wikipedia article OR a fresh Google News headline. No → drop. `event_paragraph` + `event_date` persisted into current.json.

**Quality filter (PRD-026)**: stale-obituary, marketing, 7d-recency, mainstream-source checks.

**Source-weighted sort (PRD-026)**: Google News first, then spike, then Wikipedia ITN. Sources tracked in each item's `source` field.

**Manual ops:**

```bash
# Force a fetch outside the timer
sudo systemctl start zettair-trending.service

# Bootstrap 7 days of history (skips matched hours, ~56 dumps, ~2.8 GB, 5-10 min)
sudo -u zettair python3 /opt/zettair-search/tools/fetch_trending.py --bootstrap 7

# Trim history.jsonl to last 30 days
sudo -u zettair python3 /opt/zettair-search/tools/fetch_trending.py --compact

# See what's currently on the rail
curl -s https://zettair.io/api/trending | python3 -m json.tool

# Journal (sudo required; deploy user lacks journal-read access)
sudo journalctl -u zettair-trending.service -n 60 --no-pager
sudo journalctl -u zettair-news-summary-producer.service -n 30 --no-pager
sudo journalctl -u zettair-summary-installer.service -n 20 --no-pager
```

`tools/trending_denylist.txt` — one substring per line, case-insensitive partial match against the lowercased title.

---

## Reading time + difficulty (PRD-027)

`tools/build_reading_sidecar.py` walks the docstore once, computes word + sentence + syllable counts (vowel-group heuristic) and Flesch-Kincaid grade. Buckets:

- FK ≤ 8 → `accessible` (¼ Harvey)
- 8 < FK ≤ 13 → `moderate` (½ Harvey)
- 13 < FK ≤ 20 → `technical` (¾ Harvey)
- FK > 20 → `dense` (● Harvey)
- < 150 words OR < 5 sentences → `null` (○ empty ring, "Too short to estimate difficulty")

Output: `enwiki_top1m.reading.bin`, packed binary with magic `RDT2`. ~9 MB at 1.5M docs, ~5–10 min build (CPU-bound on FK computation). Server loads into two dicts (`_reading_time`, `_difficulty`) at startup.

**Format bumps**: increment the magic (`RDT2` → `RDT3`), update `DIFF_CODE` map in both the builder and `_DIFF_CODE_TO_LABEL` in server.py, update `READING_MAGIC_EXPECTED` in setup.sh. setup.sh's magic check forces a rebuild automatically.

---

## Related entities (PRD-025)

Right-rail panel on the search results page, populated from `related.store` + `related.map`. Same-class only at output (people → people, places → places). Class headers ("Related people", "Related places", etc.) driven by `entity_class.json`. Whole panel hidden on narrow screens via CSS.

Offline pipeline lives in the **`zettair` repo** (not here), three stages:

```
build_entity_set.py  — classify titles by infobox patterns (human/place/org/work/event)
build_link_graph.py  — extract entity→entity edges from article wikitext, write CSR
build_related.py     — sampled random walks; for each entity, write top-N related to a FlatStore
```

First-run build is multi-hour (each stage ~1–2 h). Subsequent rebuilds gated by setup.sh staleness (each stage's output vs its inputs).

---

## Cite (PRD-024)

Pure client-side citation popover. Click `cite` (inline at the end of any result snippet, or on the knowledge panel) → popover with APA / MLA / Chicago / Harvard / BibTeX, each with a Copy button. Closes on Escape / click-outside / explicit ×. Single popover open at a time.

---

## Troubleshooting

**Service won't start / crashes:**
```bash
sudo journalctl -u zettair-search -n 50
```

**`zet` crashes with `docmap_load: Assertion fd >= 0`:** index files owned by root. `sudo chown -R zettair:zettair /mnt/wikipedia-source/`.

**`git pull` fails with permission denied:** `.git` was written by root. `sudo chown -R deploy:deploy /opt/zettair-search`.

**Snippets contain wrong content (text from a different article):** 4 GB TREC offset overflow — should be fixed in zettair commit `3aba055`+.

**Pills (PRD-027) all `None` in `/search` response:**
- Check `ZET_READING_SIDECAR` is set in the unit; the local-dev default doesn't resolve on prod.
- Check magic: `sudo head -c 4 /mnt/wikipedia-source/enwiki_top1m.reading.bin` should print `RDT2`.
- Check server log at startup: `sudo journalctl -u zettair-search | grep reading-sidecar`.

**`/img` returns identical bytes for different widths:** the `_rewrite_thumb_size` rewrite is over-eager. Compare requested width against `_WIKIMEDIA_ALLOWED_THUMB_WIDTHS`; whitelisted widths must pass through unchanged.

**Trending rail frozen / stale:**
- `sudo journalctl -u zettair-trending.service -n 60` (sudo required).
- Look for OOM-kill: `MemoryMax` should be `1G`; service should not be killed.
- Look for "no dump available in the last 12h": Wikimedia outage upstream; widen lookback if needed but the existing 96h should cover any realistic outage.
- Look for the file count in `/mnt/wikipedia-source/summaries/{priority,pending,done}/` to figure out where the queue is stuck.

**Click prior not affecting scores:** click_prior.bin is keyed by Zettair docid (shifts every reindex). Rebuild after a corpus refresh: `cd /opt/zettair/wikipedia && python3 build_docno_map.py /mnt/wikipedia-source/enwiki_top1m.trec && python3 build_click_prior.py && cp click_prior.bin /mnt/wikipedia-source/wikiindex/index.click_prior.bin && sudo systemctl restart zettair-search`.

**Autosuggest returns nothing:** `curl 'http://localhost:8765/suggest?q=ei&n=5'`; if empty, rebuild via `python3 build_autosuggest.py`.

**Caddy not proxying / TLS:** `sudo systemctl restart caddy`; `sudo journalctl -u caddy -n 30`.

---

## PRD index

Design decisions are recorded in `prd/`. Reading order if you're new to the codebase:

| PRD | What it covers | Status |
|-----|---------------|--------|
| PRD-006 | Click-prior ranking | Live |
| PRD-007 | Persistent worker pool — JSON Lines protocol | Live |
| PRD-008 → PRD-011 → PRD-016 | Query-biased summaries (Python ← C ← Python) | Live (PRD-016 active) |
| PRD-012 | Top-1M corpus → now 1.5M, grows after every rebuild | Live |
| PRD-013 | Session logging | Draft |
| PRD-014 → PRD-015 | URL store (replaces in-RAM dbkey map) | Live |
| PRD-017 → PRD-019 | Field-weighted BM25 → BM25F with per-field length norm and weight | Live |
| PRD-018 | Knowledge panel — summary store + offline pipeline | M1-M6 live (offline pipeline operational; Mac Mini worker `sparky`) |
| PRD-020 | Trending — spiking signal + chip rail | Live; M6 (ranking boost) and M7 (Zeitgeist page) deferred |
| PRD-021 / PRD-022 | News-spike summaries — specificity gate + Google News fallback | Live |
| PRD-023 | Feature ideas backlog | Drafted (idea backlog, not a build plan) |
| PRD-024 | Cite this | Live |
| PRD-025 | Related entities — random-walk graph | Live |
| PRD-026 | News-rail quality — strict filter + Google + Wikipedia ITN | Live |
| PRD-027 | Reading time + difficulty signal (Harvey ball) | Live |
| PRD-028 | iOS app — native client with system integration | Draft; **see handoff log at bottom of the PRD for current asks** |
