# PRD-018: Knowledge Panel — Offline-Generated Query Summaries

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-09

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

Three machines, three jobs, two artefact formats.

```
   ┌─────────────────────┐         jobs.jsonl          ┌──────────────────────┐
   │  PROD (Hetzner VPS) │ ──────────────────────────▶ │  LOCAL (Mac Mini)    │
   │                     │  (queries + top-M doc text) │                      │
   │  build_summary_     │                             │  generate_summaries  │
   │  jobs.py            │ ◀────────────────────────── │  .py                 │
   │                     │       summaries.jsonl       │                      │
   │  install_summaries  │                             │                      │
   │  .py                │                             │                      │
   │                     │                             │                      │
   │  server.py + UI     │                             │                      │
   └─────────────────────┘                             └──────────────────────┘
```

### Artefact 1: `summary_jobs.jsonl`

Generated on prod. One JSON object per line:

```json
{
  "query": "albert einstein",
  "query_norm": "albert einstein",
  "click_weight": 4837,
  "score_ratio": 1.18,
  "intent": "info",
  "results": [
    {"docid": 12345, "rank": 1, "title": "Albert Einstein", "url": "https://en.wikipedia.org/wiki/Albert_Einstein", "score": 18.4, "text": "..."},
    {"docid": 67890, "rank": 2, "title": "Theory of relativity", "url": "...", "score": 15.5, "text": "..."}
  ]
}
```

The `text` field is the full document text from `_docstore`, capped per-doc (proposed: 12 KB) so even M=5 stays well inside a 7B model's context.

Two flavours of the file:

- `summary_jobs_full.jsonl` — head N queries, regenerated on corpus refresh.
- `summary_jobs_delta.jsonl` — queries newly in the head that aren't in `summaries.store` yet.

### Artefact 2: `summaries.jsonl`

Generated on the Mac Mini. One JSON object per line:

```json
{
  "query_norm": "albert einstein",
  "summary_md": "**Albert Einstein** (1879–1955) was a German-born theoretical physicist...\n\n- Developed the theory of relativity\n- Won the 1921 Nobel Prize in Physics\n- ...",
  "model": "llama-3.1-8b-instruct",
  "generated_at": "2026-05-09T14:32:11Z",
  "source_docids": [12345, 67890, 11122]
}
```

### Storage on prod

`summaries.store` (binary blobs, concatenated) + `summaries.map` (JSON dict from `query_norm` → `{offset, length}`). Same FlatStore pattern as `_docstore` and `_urls_store` — `os.pread()` per access, no in-memory hash beyond the offset map.

### Server changes

`server.py` gains a `_summaries_store` instance loaded at startup. `/search` looks up `query_norm` in the offset map; if present, includes `"summary": "<markdown>"` in the response. If absent, the field is omitted (or `null`). No fallback, no live generation.

A small admin endpoint `/admin/summaries-stats` returns count, oldest entry by `generated_at`, and pending-delta count (queries in the head but not in the store).

### Front-end

`index.html` renders the panel above the result list when `summary` is present. Markdown → HTML via a small renderer (or just `marked` if we already have it). Visual treatment matches Google's knowledge panel: card with subtle border, query echoed as title, summary prose, key-facts bullets, "summary based on top results" footer.

### Query normalisation

`query_norm` is `lower().strip()` with collapsed inner whitespace. Same normalisation in `build_summary_jobs.py`, `generate_summaries.py`, `install_summaries.py`, and `server.py` lookup. Defined once in a shared helper to avoid drift.

### Nav-vs-info filter

`build_summary_jobs.py` reuses the rank1/rank2 score-ratio classifier from `intent.py`. Queries with ratio ≥ threshold (proposed: 2.0) are skipped — the top result is the answer; a panel adds nothing. Threshold is configurable; we'll tune it from the first run's bucket distribution.

---

## Operational flow

**Weekly (or on corpus refresh):**

1. On prod: `build_summary_jobs.py --top 2000 --m 5 --out summary_jobs_full.jsonl`. Reads `/suggest` for query pool, runs each through `/search`, filters out nav by score ratio, pulls top-M doc text from `_docstore`. Writes JSONL.
2. Pull `summary_jobs_full.jsonl` to the Mac Mini (rsync / scp).
3. On Mac Mini: `generate_summaries.py --in summary_jobs_full.jsonl --out summaries_full.jsonl --model <local>`. One model call per query, structured prompt, retries on malformed output.
4. Push `summaries_full.jsonl` back to prod.
5. On prod: `install_summaries.py --in summaries_full.jsonl --rebuild`. Builds new `summaries.store` + `summaries.map`. Atomic rename. `systemctl restart zettair-search`.

**Daily:**

1. On prod: `build_summary_jobs.py --delta --out summary_jobs_delta.jsonl`. Same as step 1 but only for queries currently in the head pool that are *not* in `summaries.map`.
2. Pull / generate / push as above.
3. On prod: `install_summaries.py --in summaries_delta.jsonl --merge`. Appends to `summaries.store`, updates `summaries.map`. Atomic rename. Restart.

The pull and push directions are intentionally manual to start — we'll automate them once the shapes are stable.

---

## Open questions

- **Prompt format**: what does the model actually receive? Proposed: system prompt with "you are writing a Wikipedia-style knowledge-panel summary; ground every claim in the source documents below; use markdown bold for the head term; keep to 80–150 words plus 3–5 key-fact bullets". Source docs concatenated with `=== DOC 1: <title> ===` separators. To be tuned empirically.
- **Per-doc text cap**: 12 KB seems sane for M=5 on a 7B-with-128k model; tighter on smaller-context models. Configurable.
- **Summary refresh policy**: do we re-generate when the rank-1 doc for a query changes? (Probably yes, as a separate "stale" filter in delta job.) For v1, only re-generate when the doc text itself changes meaningfully — out of scope for v1, accept staleness.
- **Failure mode for malformed model output**: skip the query, log it, retry next delta. Don't ship a half-baked summary.
- **Delete path**: a query that drops out of the head doesn't need its summary actively removed; the offset map is small. Garbage-collect on rebuild only.

---

## Milestones

1. **M1 — server-side plumbing.** FlatStore for summaries, `/search` returns the field when present. Empty store, no panel rendered. Verifiable: dummy entries injected, response carries `summary`, front-end shows the card.
2. **M2 — front-end panel.** `index.html` renders a Google-style knowledge panel above results when `summary` is present. CSS, markdown rendering, mobile-friendly layout.
3. **M3 — `build_summary_jobs.py` (prod).** Produces `summary_jobs_full.jsonl` for top N queries. Includes nav filter.
4. **M4 — `generate_summaries.py` (Mac Mini).** Calls local model, produces `summaries.jsonl`. Tuned prompt.
5. **M5 — `install_summaries.py` (prod).** Builds store + map, atomic install, restarts service.
6. **M6 — delta loop.** Both `build_` and `install_` gain `--delta` / `--merge` modes. Daily cron on prod.
7. **M7 — admin/observability.** `/admin/summaries-stats` endpoint, structured logging on hits/misses, dashboard for coverage and median age.

M1 and M2 are independently testable with hand-written summary entries. M3+ is the offline pipeline.

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
