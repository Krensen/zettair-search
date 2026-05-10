# PRD-018: Knowledge Panel — Offline-Generated Query Summaries

**Status:** M1+M2 live on prod (server FlatStore + animated front-end panel, hand-fed demo summaries). M3-M7 in flight via a queue-based architecture: prod drops job files in `pending/`, Mac Mini (separate `Krensen/zettair-summariser` repo) rsyncs them, generates with a local model, rsyncs results back to `done/`, prod-side installer drains into the FlatStore.
**Author:** metabot
**Date:** 2026-05-09 (rev 2026-05-11: queue architecture, separate Mac Mini repo)

---

## Problem

Searches on zettair return a ranked list of Wikipedia articles. For most informational queries, what the user actually wants is a one-paragraph answer — the kind of thing Google shows in a knowledge panel above the blue links. Today they have to click through and skim.

We can't generate these summaries live: even a small local model is too slow to gate on, and the production VPS doesn't have a GPU. But we don't need to. The query distribution is heavily Zipfian — the top few thousand queries cover the bulk of traffic — and the source documents barely change. So summaries can be precomputed offline, shipped to the server, and served as cached strings.

The pieces we need:

1. A way to **decide which queries deserve a summary** (skip navigational queries — the top result *is* the answer).
2. A way to **export the source material** (top-M full document texts per query) from prod to a local model machine.
3. A way to **generate the summaries** offline on the local model machine.
4. A way to **install summaries back into prod** so `/search` returns them alongside results.
5. A **delta loop** so new queries that climb into the popular list eventually get summaries without rebuilding the world.

---

## Goal

When the user issues a query that has a precomputed summary, the response includes a `summary` field containing 1–2 paragraphs of grounded prose plus a short bulleted list of key facts. The front-end renders this as a knowledge-panel block above the result list.

When the user issues a query without a precomputed summary, the response works exactly as it does today — no panel, just the blue links. No live summary attempt, no fallback model call, no perceptible latency hit.

Specifically:

- Top ~2,000 head queries (by autosuggest click count) have summaries within a week of corpus refresh.
- New queries that climb into the head list get summaries within ~24 hours of the next delta job.
- Coverage and freshness are visible — `/admin/summaries-stats` (or similar) reports count, oldest entry, and N delta-pending.
- Adding a summary is "drop a JSONL file in the right place, restart the server" — same operational shape as the docstore and URL store.

---

## Why offline batch (not live generation)

- **Latency**: a 7B local model takes 1–3 s per summary even on M-series Apple silicon. p95 search latency today is ~50 ms. Adding summary generation to the request path is a 30–60× regression.
- **Cost shape**: most informational query volume concentrates on the head. Summarising the head once and caching wins on every repeat hit. Live generation pays the cost on every hit.
- **Hardware locality**: the VPS is CPU-only and tight on RAM. The user already runs a Mac Mini with a local model. Putting the GPU work where the GPU is.
- **Freshness budget**: Wikipedia articles change slowly relative to query distribution. A daily delta job is well within tolerance.
- **Blast radius**: a bad summary in cache is fixable by re-running the offline job. A live model that goes off the rails is a per-request quality problem.

This matches what Google actually does for the bulk of its knowledge panels — pre-extracted, cached, served as static strings.

---

## What is *not* in scope

- Live model calls from the request path. Ever.
- Per-result summaries. The summary is per-query, attached above the result list, not per-document. (Per-doc query-biased snippets already exist via `summarise.py`.)
- Image selection, infobox extraction, structured data. Plain prose + bullets only.
- Multi-turn / chat. One query → one summary string.
- Disambiguation. If the query is ambiguous, the summary is whatever the model produces given the top-M docs; no ambiguation UI.
- Citations or per-fact source linking. The prose is grounded in the top-M docs, but the rendered panel just notes "summary based on top results" with no per-sentence cites.

---

## Design

Three loose loops, files as the medium. Producer on prod, worker on Mac Mini, installer on prod. Each loop runs independently — the Mac Mini can be offline for a week and prod doesn't care; pending jobs just queue. Multiple Mac Minis can poll the same queue and coordinate via filesystem semantics (rsync's per-file rename is the lock).

```
                    ┌───────────────────────────────────────────────┐
                    │  PROD VPS  /mnt/wikipedia-source/summaries/   │
                    │                                               │
                    │   ┌───────────────────────────────────────┐   │
                    │   │  producer (cron, every few hours)     │   │
                    │   │  ─ tools/build_summary_jobs.py        │   │
                    │   │    head 2000 + live-enqueue           │   │
                    │   │  ─ writes pending/<query_norm>.json   │───┼─┐
                    │   └───────────────────────────────────────┘   │ │
                    │                                               │ │ rsync
                    │   ┌───────────────────────────────────────┐   │ │ over ssh
                    │   │  installer (cron, every ~5 min)       │   │ │ (sparky)
                    │   │  ─ tools/install_summaries.py         │◀──┼─┼─┐
                    │   │  ─ drains done/, calls                │   │ │ │
                    │   │    summaries_admin.py add,            │   │ │ │
                    │   │    restarts service when needed       │   │ │ │
                    │   └───────────────────────────────────────┘   │ │ │
                    └───────────────────────────────────────────────┘ │ │
                                                                      │ │
                                ┌─────────────────────────────────────┘ │
                                ▼                                       │
                    ┌───────────────────────────────────────────────┐   │
                    │  MAC MINI  (Krensen/zettair-summariser repo)  │   │
                    │                                               │   │
                    │   ┌───────────────────────────────────────┐   │   │
                    │   │  poll.py (launchd timer)              │   │   │
                    │   │  ─ rsync pending/ → local inbox       │   │   │
                    │   │  ─ generate, write outbox             │   │   │
                    │   │  ─ rsync outbox → prod done/          │───┼───┘
                    │   └───────────────────────────────────────┘   │
                    │   ┌───────────────────────────────────────┐   │
                    │   │  generate.py + prompt.py              │   │
                    │   │  ─ ollama / llama.cpp local model     │   │
                    │   └───────────────────────────────────────┘   │
                    └───────────────────────────────────────────────┘
```

### Directory layout on prod

Under `/mnt/wikipedia-source/summaries/`, all owned by `zettair`, group `summariser` (a new group that includes `zettair` and `sparky`), mode `2775` (setgid so files inherit the group):

- `pending/` — `<query_norm>.json` files dropped by the producer. Workers claim them via rsync `--remove-source-files`; the rename-then-unlink is the lock.
- `done/` — `<query_norm>.md` files dropped by the worker after successful generation. Plain markdown, no metadata wrapper.
- `installed/` — `<query_norm>.md` files moved here once the installer has merged them into the FlatStore. Audit trail; prune older than 90 days.
- `errors/` — `<query_norm>.error.json` for jobs the worker explicitly failed (model output didn't parse, hit safety filter, etc.). For manual retry / inspection.

### Pending job format (`pending/<query_norm>.json`)

```json
{
  "schema_version": 1,
  "query": "Albert Einstein",
  "query_norm": "albert einstein",
  "click_weight": 4837,
  "score_ratio": 1.18,
  "intent": "info",
  "created_at": "2026-05-11T01:23:45Z",
  "source": "bulk",
  "results": [
    {"docid": 12345, "rank": 1, "title": "Albert Einstein", "url": "https://en.wikipedia.org/wiki/Albert_Einstein", "score": 18.4, "text": "..."},
    {"docid": 67890, "rank": 2, "title": "Theory of relativity", "url": "...", "score": 15.5, "text": "..."}
  ]
}
```

`source` is `"bulk"` (came from build_summary_jobs scanning the head pool) or `"live"` (came from a real user query). `text` is the cleaned docstore content capped per-doc at 12 KB.

### Done summary format (`done/<query_norm>.md`)

Plain markdown. No metadata wrapper. Filename carries `query_norm`; body is what the installer feeds to `summaries_admin.py add`.

```
**Albert Einstein** (1879–1955) was a German-born theoretical physicist...

- Developed the theory of relativity
- Won the 1921 Nobel Prize in Physics
- ...
```

### Producer — `tools/build_summary_jobs.py` (on prod)

Run by a systemd timer every few hours.

**Bulk mode (default):** walks the top 2000 head queries from autosuggest (same as `intent.py`'s `fetch_top_queries`), classifies each via the rank1/rank2 score ratio, drops `pending/<query_norm>.json` for any informational query (ratio < 2.0) that:

- isn't already in `summaries.map`,
- doesn't already have a file in `pending/`, `done/`, or `installed/`.

For each candidate, the producer hits `/search` to get top-M results, then reads the cleaned text from `_docstore` (or from the snippet store as fallback), trims to 12 KB per doc, and writes the job file atomically (`pending/foo.json.tmp` → rename to `pending/foo.json`).

**Live mode:** server.py's `/search` handler, after returning results to the user, appends to a `live_queue.jsonl` log when the response had no summary AND `score_ratio < 2.0`. Fire-and-forget via `asyncio.create_task` on a thread executor so the request path isn't blocked. A separate `digest_live_queue.py` cron promotes high-confidence queries (≥ N unique-session hits in last 24h) to proper `pending/*.json` job files. Coalesces dupes — a popular query that hits 100 times produces one job.

### Worker — `Krensen/zettair-summariser` repo on Mac Mini

Separate public repo. Files-as-protocol means the worker only needs to know the directory schema, not how prod produces or installs.

```
zettair-summariser/
├── README.md
├── setup.sh                       — provision a fresh Mac Mini (brew, pip, launchd plist)
├── poll.py                        — top-level loop, run by launchd every N min
├── generate.py                    — wraps the local model (ollama / llama.cpp), retries
├── prompt.py                      — prompt template + output parsing
├── config.example.toml            — copy to config.toml on first setup; SSH host, model id, M
├── com.zettair.summariser.plist
└── tests/fixtures/                — known job JSONs + expected summary structures
```

`poll.py` flow (one call):

1. `rsync -av --remove-source-files sparky@prod:/mnt/wikipedia-source/summaries/pending/*.json /local/inbox/`. The `--remove-source-files` step is per-file atomic on prod's filesystem — multiple workers racing each claim a disjoint subset.
2. For each `inbox/*.json`: call `generate.py` to produce a markdown summary; write to `/local/outbox/<query_norm>.md`. If model output doesn't validate, write `/local/errors/<query_norm>.error.json` instead.
3. `rsync -av --remove-source-files /local/outbox/*.md sparky@prod:/mnt/wikipedia-source/summaries/done/` and the same for `errors/`.
4. Move `inbox/*.json` to `inbox-processed/` locally as an audit trail.

Failure of any rsync step is idempotent: files stay where they are and the next run retries.

### Installer — `tools/install_summaries.py` (on prod)

Run by a systemd timer every ~5 minutes.

1. Acquire flock on `/mnt/wikipedia-source/summaries/installer.lock`.
2. List `done/*.md`.
3. For each: derive `query_norm` from the filename, read body, call `summaries_admin.py add` to update the FlatStore. Move to `installed/`.
4. If at least one new summary was installed, `systemctl restart zettair-search` so the server reloads the offset map.
5. Release lock.

A narrow sudoers entry lets the `zettair` user run `systemctl restart zettair-search` without password — the installer otherwise needs no root.

### Storage on prod (M1)

`summaries.store` (binary blobs, concatenated) + `summaries.map` (JSON dict from `query_norm` → `[offset, length]`). FlatStore. Read by `os.pread()`. Already implemented.

### Server changes (M1)

`server.py` returns `summary` field in `/search` responses when present. Already implemented. Live-mode logging is the only addition: append to `live_queue.jsonl` when `score_ratio < 2.0` and no summary was returned.

### Front-end (M2)

Knowledge panel with shimmer skeleton + cascade reveal when `data.summary` is present. Already implemented.

### Query normalisation

`query_norm(s)` is `lower().strip()` with collapsed inner whitespace. Defined in `server.py`, re-implemented identically in `tools/summaries_admin.py`, the new producer/installer scripts, and `zettair-summariser/poll.py`. Trivial enough that re-implementing is cheaper than pulling a shared module across two repos. If the function ever gets more complex, promote it to a tiny shared file synced via the schema_version bump.

### Nav-vs-info filter

`build_summary_jobs.py` reuses the rank1/rank2 score-ratio classifier from `intent.py`. Queries with ratio ≥ 2.0 are skipped — the top result is the answer; a panel adds nothing. Threshold is configurable.

### SSH and permissions

The `sparky` user already exists on prod (home `/home/sparky`) with SSH key auth set up. Required additional setup:

- Create group `summariser`, add `zettair` and `sparky` to it.
- Create `/mnt/wikipedia-source/summaries/{pending,done,installed,errors}/`, owner `zettair`, group `summariser`, mode `2775`.
- `sparky` can read `pending/`, write `done/` and `errors/`. Cannot touch `installed/` or the FlatStore directly — only the prod-side installer has those permissions.
- Installer cron runs as `zettair`. Narrow `/etc/sudoers.d/zettair-installer` granting `NOPASSWD: /bin/systemctl restart zettair-search` only.

---

## Operational flow

Three independent timers, no orchestration between them.

**On prod (every few hours):** `build_summary_jobs.py --mode bulk` scans the top-N head queries, drops new `.json` files in `pending/`. Idempotent.

**On Mac Mini (launchd, every N minutes):** `poll.py` rsyncs pending down (claiming via `--remove-source-files`), generates summaries with the local model, rsyncs done back. If the box is offline this just doesn't run; jobs queue on prod.

**On prod (every ~5 minutes):** `install_summaries.py` drains `done/` into the FlatStore, restarts the service when anything new lands.

**Live queue (continuous):** server.py logs informational-but-unsummarised queries to `live_queue.jsonl`; a cron promotes high-confidence ones to `pending/`.

All loops fail safely: a stuck Mac Mini stretches the queue but doesn't break prod; a failed generation lands in `errors/` and can be retried by hand; a partial rsync just retries next tick.

---

## Open questions

- **Prompt format**: what does the model actually receive? Proposed: system prompt with "you are writing a Wikipedia-style knowledge-panel summary; ground every claim in the source documents below; use markdown bold for the head term; keep to 80–150 words plus 3–5 key-fact bullets". Source docs concatenated with `=== DOC 1: <title> ===` separators. To be tuned empirically.
- **Per-doc text cap**: 12 KB seems sane for M=5 on a 7B-with-128k model; tighter on smaller-context models. Configurable.
- **Summary refresh policy**: do we re-generate when the rank-1 doc for a query changes? (Probably yes, as a separate "stale" filter in delta job.) For v1, only re-generate when the doc text itself changes meaningfully — out of scope for v1, accept staleness.
- **Failure mode for malformed model output**: skip the query, log it, retry next delta. Don't ship a half-baked summary.
- **Delete path**: a query that drops out of the head doesn't need its summary actively removed; the offset map is small. Garbage-collect on rebuild only.

---

## Milestones

1. **M1 — server-side plumbing.** ✅ Live. FlatStore for summaries, `/search` returns `summary` field when present, env vars wired through the systemd unit. Validated with hand-fed demo summaries.
2. **M2 — front-end panel.** ✅ Live. Shimmer skeleton + cascade reveal in `index.html` when `data.summary` is present. Missing summaries fall through to the existing top-result panel.
3. **M3 — queue scaffolding (prod).** Create `/mnt/wikipedia-source/summaries/{pending,done,installed,errors}/` with the right group/perms via setup.sh. Add `summariser` group, add `sparky` to it. Stub `tools/build_summary_jobs.py` with `--mode bulk` only (no live mode yet). One systemd timer.
4. **M4 — Mac Mini repo + worker (zettair-summariser).** New `Krensen/zettair-summariser` repo. `setup.sh` provisions ollama or llama.cpp, pip deps, SSH config, launchd plist. `poll.py` does the rsync ↔ generate ↔ rsync loop. `prompt.py` carries the prompt template; `generate.py` wraps the model call with retries.
5. **M5 — installer (prod).** `tools/install_summaries.py` drains `done/`, calls `summaries_admin.py add`, restarts service. Systemd timer every 5 min. flock around the batch.
6. **M6 — live queue.** server.py logs unsummarised informational queries to `live_queue.jsonl`. `tools/digest_live_queue.py` cron promotes high-confidence queries to `pending/`. Counter de-dupes.
7. **M7 — observability.** `/admin/summaries-stats` endpoint: total count, oldest entry, queue depths (pending/done/installed/errors). Structured logs from each timer write per-batch totals so coverage growth is greppable.

M1+M2 are independently testable with hand-fed summaries (done). M3-M7 is the automated pipeline. M3 and M5 can land in `zettair-search` in any order; M4 lands in the new `Krensen/zettair-summariser` repo.

---

## Success criteria

- ≥ 80% of informational queries (by click-weighted volume) have a summary within one week of corpus refresh.
- p95 search latency unchanged (offline pipeline cannot regress the live path).
- A summary that's wrong can be killed in one command (`install_summaries.py --delete <query>`).
- Adding a new field to the summary JSON (e.g. `confidence`, `model_version`) is one diff in `install_summaries.py` and `server.py`, not a format break.

---

## Non-decisions deferred

- Whether to embed summaries in `_docstore` itself (no — separate store keeps the docstore immutable per corpus refresh).
- Whether `query_norm` should fold synonyms / aliases (no — exact lookup; aliasing is a layer above).
- Whether to write summaries for nav queries too (no — adds noise above an answer that's already there).
- Whether the panel should expose "regenerate this summary" to users (no — closed-loop content).
