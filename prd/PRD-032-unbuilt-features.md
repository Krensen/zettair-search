# PRD-032: Unbuilt Features — Catalogue of Deferred Work

**Status:** Living document
**Author:** metabot
**Date:** 2026-06-05

---

## Purpose

A single index of every concrete, named feature or follow-up that has
been thought through (in another PRD or in conversation) but is not
yet built. Avoids losing ideas between sessions.

This is **not** a build plan — it does not prioritise, schedule, or
sequence. Each entry links back to the PRD where the idea was first
proposed, so the design context can be re-read in place rather than
duplicated here. Tick items off (move them to a "Shipped" section
at the bottom, with a date) when they land.

Curated 2026-06-05 by sweep over PRD-001…PRD-031 plus the in-session
conversation up to that point.

---

## Ranking improvements

| Item | Source | Notes |
|---|---|---|
| Fold per-field length sidecars (`field_lengths.bin`, `field_stats.bin`) into the docmap | PRD-019 M3a | Eliminates coherence hazard between two files that must agree. |
| Remove dead `g_field_boost[]` per-occurrence boost code | PRD-019 M4 | Superseded by BM25F; left in place. |
| Per-field IDF (`ZET_FIELD_IDF=on`) | PRD-019 M5 | Currently shared corpus IDF across fields. |
| Additive freshness boost: trending titles get a small BM25 add | PRD-020 M6 | Same shape as click-prior. Would address `iran` → current-events surfacing. |
| Related-entity graph as a ranking signal | PRD-025 M8 | The graph already exists; not yet wired into scoring. |
| α sensitivity sweep for `ZET_CLICK_ALPHA` | PRD-006 | Currently 0.05, eyeballed. |

## Search recall

| Item | Source | Notes |
|---|---|---|
| Fuzzy / substring matching in autosuggest | PRD-005 | "tower" → "Eiffel Tower" et al. |
| SymSpell "did you mean" spell correction | PRD-009 (Draft) | Never built. |
| Unicode NFKD normalisation in summariser tokeniser | PRD-030 #2 | So "Beyonce" matches "Beyoncé", "Sao Paulo" matches "São Paulo". |

## News pipeline polish

| Item | Source | Notes |
|---|---|---|
| Refresh `:news` summary when rank-1 doc changes for a query | PRD-018 M5 | Detect via index/click drift. |
| Re-generate biographical summary when source article body changes meaningfully | PRD-018 | Staleness signal needed. |
| Content-hash refresh policy: refresh `:news` within 48h if source event paragraph changed | PRD-021 M5 | Reduces "stale recent-news" feel. |
| Multiple event paragraphs per :news job (2-3 instead of best one) | PRD-021 M6 | More angles on a story. |
| Split rail into "Trending news" (specificity-gated) vs "Popular searches" (raw spikes) | PRD-021 | Two visual lanes. |
| Per-headline citation footer "Sources: BBC, Guardian, …" | PRD-022 M4 | Trust signal. |
| Rename rail from "Trending" to "News" | PRD-026 | Sources are mostly news anyway. |
| News-panel matching: fuzzy match query string against rail items | In-session (2026-06-05) | Today `putin` does not fire the panel even when `vladimir putin` is on the rail. Either fuzzy match or pivot to docno-keyed `:news` lookup. |

## Snippets

| Item | Source | Notes |
|---|---|---|
| Hand-curated 30-pair snippet eval | PRD-030 §Quality measurement | Deferred during steps A-G; corpus rebuild has now landed so the eval would catch any regressions and validate the layered fixes. |

## Observability + infra

| Item | Source | Notes |
|---|---|---|
| `/admin/summaries-stats` endpoint | PRD-018 M7 | Counts, ages, queue depths. (Some of this is now in `/health/news` — that endpoint covers the read-only public-safe slice; admin endpoint would be richer.) |
| Session logging: sid + ip + full results list in query log; clicks linked by sid | PRD-013 (Draft) | Unblocks A/B-able ranking experiments. |
| Log rotation for queries/clicks/crashes | PRD-013 | Files grow unbounded today. |

## Feature backlog (PRD-023 cherry-picks)

| Item | Source | Notes |
|---|---|---|
| Compare view (X vs Y) | PRD-023 #2 | Precompute top-1000 entity pairs. |
| Per-article timeline view | PRD-023 #3 | Date extraction from article body. |
| Saved searches / reading lists | PRD-023 #6 | localStorage v1; backend later. |
| "Related but different" via click-log aggregation | PRD-023 #7 | Requires session logging (PRD-013) first. |
| Article quality badge (Featured / Good / Stub) | PRD-023 #8 | Wikipedia exports this; small UI signal. |
| Image-grid result mode | PRD-023 #10 | Alt view, image-store already exists. |
| Time-machine "as of date" search | PRD-023 #11 | Hard — requires historical index. |
| Voice / audio mode | PRD-023 #12 | Web Speech API is free. |
| AI ask-a-question on top of search | PRD-023 #1 | Requires hosted inference or head-cache infrastructure first. |

## Small cleanups

| Item | Source | Notes |
|---|---|---|
| Delete `build_dbkey_map.py` + `enwiki_top1m.dbkeys.tsv` | PRD-015 cleanup | Superseded by URL store. |
| Mark PRD-014 superseded | PRD-015 cleanup | Documentation hygiene. |
| Drop `--summary=plain` from zet worker args | PRD-011 followup | Perf win identified when PRD-016 superseded the C summariser; the worker arg was left behind. |
| Move zettair-side image extractor default from `/300px-/` to `/500px-/` | PRD-028 ask | Coordinated with iOS work; zettair-repo change. |

## Bigger draft PRDs

These are full PRDs that exist in draft form but haven't been scoped
into a build. Listed here for completeness; pick one of these and
the PRD itself is the spec.

| Item | Source | Notes |
|---|---|---|
| iOS app v1 | PRD-028 | ~5 weeks effort; "server-side handoff log" at bottom of the PRD is the running ask list. |
| News timeline / `/news` page | PRD-029 | ~8-11 days; soft-launch path proposed. |

---

## How to use this document

- **Skim once a week.** Look for things you now have appetite for, or that have become more pressing because of unrelated changes.
- **Move items to "Shipped" at the bottom** with a date and PR link when they land. Do not delete — the audit trail is part of the value.
- **Add items as they come up in conversation.** This file is the dump for "good idea, not now" thoughts so they don't get lost between sessions.

---

## Shipped (audit trail)

_None yet (this PRD was created 2026-06-05; the items above are the
starting backlog. Future shipped items will be moved here from above
with a date and the PR that delivered them.)_
