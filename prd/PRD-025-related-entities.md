# PRD-025: Related Entities — Random-Walk Graph of Wikipedia Entity Articles

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-13

---

## Problem

When a user lands on a Wikipedia article via search, the natural follow-up
is "what's related?" — other people, places, organisations, works that
share a meaningful connection to this one. Wikipedia's existing
affordances for this are weak:

- **"See also"** — editor-curated, inconsistent, often a one-line list at
  the bottom of an article. Useless for short articles, missing entirely
  for many.
- **"What links here"** — backlinks, raw and ungroomed. Hundreds of
  unranked entries for popular articles, mostly noise.
- **In-article links** — embedded throughout the body, hard to scan as a
  list, biased toward whatever the editor chose to link rather than what's
  most relevant.

None of these surfaces as a clean "you might also like X, Y, Z" panel that
extends a user's session and helps them discover the next thing.

A ranked, curated list of related entities, built from the structure of
Wikipedia itself, is something Wikipedia knows how to compute but doesn't
expose. We can build it cleanly, and it's the highest-impact retention
feature we could ship — exploration is the deepest engagement loop.

This pattern has shipped successfully at scale before (Bing's original
Related Searches was built exactly this way against Wikipedia for named
entities). We're rebuilding a proven approach, not inventing one.

---

## Goal

Precompute, per entity-article in our corpus, a ranked list of related
entity-articles. Surface the top ~6–10 as a right-rail "Related
entities" panel on the search results page. Click → run a search on
the related entity.

The graph and walks are computed offline on prod at index-rebuild time.
Storage is a FlatStore alongside the existing summary/snippet stores.
The server loads it once at startup and projects related lists into
search responses with negligible latency.

Restrict v1 strictly to **named-entity articles**. Non-entity nodes
(disambiguation pages, list pages, year articles, set-index articles,
concept articles) are excluded from the graph itself — not the walk
output — so disambiguation/trash-neighbour problems are solved at
graph-construction time, not at output time.

---

## Non-goals

- **Per-query related searches.** This is per-article (per-entity), not
  per-query. A query that doesn't match an entity article gets no
  related panel — that's fine; we have a knowledge panel for that case
  already.
- **Concept-article relations.** "Quantum mechanics" or "Photosynthesis"
  don't get related entities in v1. The signal is different (subtopics
  vs related entities) and the graph would need different filtering.
  v2 territory.
- **Live updates.** The graph is rebuilt with the index. Entities added
  to Wikipedia between rebuilds don't get a related panel until the next
  rebuild.
- **Personalisation.** Same related list for every user.
- **Multilingual.** en.wikipedia only. Wikidata is multilingual but we
  scope to one language for now.
- **A dedicated "related" landing page** (`/related/Mark_Carney`).
  Just the right-rail panel on search results. A standalone explorer
  is a future PRD.

---

## High-level architecture

```
At index-rebuild time (one-shot, prod CPU job):

  Wikidata dump  ─────┐
                      ▼
              parse_entity_filter.py
              (Q-number, instance-of) → entity Q-set
                      │
                      ▼
                docno → is_entity? lookup
                      │
  enwiki wikitext ────┤
                      ▼
              build_link_graph.py
              parse [[Links]] from wikitext for entity docnos
              keep edges where BOTH endpoints are entities
              output: graph.csr (compact sparse-row binary)
                      │
                      ▼
              random_walk.py
              for each entity-node:
                run K walks of length L starting from this node
                aggregate target frequencies
                keep top-N (default 20)
              output: related.flatstore + related.map
                      │
                      ▼
At query time (live, no extra work per query):

  server.py
    loads related.map at startup
    for the top result of each search:
      if docno is an entity AND has a related entry:
        attach related[] to the response
    cost: O(1) map lookup + O(20) projection
                      │
                      ▼
  frontend (index.html)
    renders right-rail "Related entities" panel
    each chip is the entity title; click runs a search
```

Everything offline runs on prod. Mac Mini is not in this loop.

---

## Pieces

### 1. Identifying entities (Wikidata-driven)

Every Wikipedia article has a Wikidata Q-number. Wikidata stores an
`instance-of` (P31) property that classifies the entity:

- Q5 — human
- Q43229 — organization (rolls up: company, government, NGO, sports team)
- Q515 — city / Q3957 — town / Q486972 — settlement (rolls up most places)
- Q11424 — film / Q571 — book / Q482994 — album / Q4830453 — business …
- Q1656682 — event

We include articles whose P31 transitively rolls up to one of a
hand-curated allowlist of root classes (human, organisation, place,
work, event). Wikidata's class hierarchy via P279 (subclass of) makes
the rollup straightforward.

Wikidata dumps are smaller than enwiki (~80 GB JSON, but we only need
P31/P279 for the Q-numbers our docnos use). One-shot extract: stream
the dump, filter to our docno set, emit `{docno: q_number,
entity_class: <one of {human, place, organisation, work, event}>,
is_entity: bool}` per article.

Expected entity coverage of our 1.5M corpus: 30-50%, so 500k-750k
entity articles.

**Risk: Wikidata coverage gaps.** Some Wikipedia articles don't have a
Wikidata Q-number (rare) or have P31 missing/wrong. Acceptable; those
articles just won't be entities. ~1-2% loss.

### 2. Building the link graph

The docstore already has cleaned article text per docno, but we want
the **wikitext** for clean outlink extraction (links are `[[Target]]`
in wikitext, hard to reconstruct from cleaned text).

Two options:

**A. Use the existing TREC docstore body text.** Outlinks were partially
preserved during TREC conversion as visible text, but the link
structure was stripped. Reconstructing is lossy.

**B. Process the raw enwiki dump once at rebuild time.** Same dump
`wiki2trec.py` already reads. Add a parallel pass that extracts
`[[Target]]` patterns into an edge list.

**Decision: B.** We have the dump; one extra streaming pass over the
~80 GB bz2 file costs ~1-2 hours of CPU. Output is an edge list
`(source_docno, target_docno)`.

Edge filtering:
1. Drop edges where source isn't an entity.
2. Drop edges where target isn't an entity.
3. Drop self-edges.
4. Drop duplicate edges (multiple `[[Mark_Carney]]` mentions in one
   article count as a single edge).

Expected graph: 500k-750k nodes, 50-150M edges. Stored as CSR
(compressed sparse row): one offset array (`int64`), one neighbour
array (`int32`). ~600 MB-1 GB on disk.

### 3. Random walk

For each entity node, compute a ranked list of related entities. Two
candidate algorithms:

**Personalised PageRank (PPR).** For each source node, the PPR vector
gives a stationary distribution biased toward starting from that
node. Top-K entries are the related list. Mathematically clean;
empirically good on dense graphs.

**Sampled random walks.** From the source, run K walks of length L
(e.g. K=1000, L=8), aggregate target visits, take top-N. Simpler to
implement; well-suited to sparse graphs; embarrassingly parallel.

**Decision: sampled random walks for v1.** Per-source compute is
trivially parallelisable, total wall-clock is hours not days, and
the implementation fits in ~200 lines of Python (or 50 lines of
NumPy if we want it fast). PPR can be a future optimisation if
quality demands it.

Parameters (initial; tune in M5):
- K = 1000 walks per source
- L = 8 hops per walk
- Top-N = 20 related per source
- α = 0.15 restart probability per hop (so walks don't drift too far)

Walks restart at the source with probability α, otherwise pick a
uniform-random outlink. Restart prevents long random walks from
washing out the source-context.

### 4. Aggregation and ranking

For each source S, after K walks:
- Each visited target gets a count.
- Score = log(count + 1) so a target visited 100× scores less than 100×
  more than one visited 1×.
- Normalise so the top result's score is 1.0 (cosmetic; helps tuning).
- Keep top-N by score, filtered to: not the source, score > 0.05 of top
  (drops noise).

Per-source output: list of `(target_docno, score)` pairs, length ≤ 20.

### 5. Storage

FlatStore exactly like `summaries.store` / `snippets.store`:

- `related.store` — concatenated binary records, one per source
  docno. Record format: a length-prefixed JSON array
  `[["Liverpool_F.C.", 1.0], ["Manchester_United", 0.87], ...]`.
- `related.map` — `{docno: [offset, length]}`.

Estimated size: 750k entries × ~400 bytes/record = ~300 MB. Fits
trivially.

Server reads the map at startup (~50 MB in RAM). Lookups are O(1)
+ one `pread()` for the body. Latency: sub-millisecond.

### 6. Server-side serving

`server.py` extends the `/search` response. For the top result, if
docno is an entity-article and has a related list, attach:

```json
"related": [
  {"docno": "Liverpool_F.C.", "title": "Liverpool F.C.", "score": 1.0},
  {"docno": "Manchester_United", "title": "Manchester United", "score": 0.87},
  ...
]
```

10 items max. Computed once per request, projected from a cached
in-memory store.

No related list → field omitted, frontend hides the right-rail panel.

### 7. Frontend — right-rail panel

The results page currently has a centre column (results) + small
right-aligned thumbnails per result. The new right-rail "Related
entities" panel sits in a fixed column at right, top-aligned with the
first result.

```
┌────────────────────────────────────────────────────────────┐
│  [knowledge panel]                                          │
│  [result 1]                          ┌──────────────────┐  │
│  [result 2]                          │ Related entities │  │
│  [result 3]                          │  • Liverpool F.C.│  │
│  [result 4]                          │  • Man United    │  │
│  …                                   │  • Pep Guardiola │  │
│                                      │  • Premier League│  │
│                                      │  …               │  │
│                                      └──────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

Each item is a clickable link with the entity title. Click → searches
that entity (same `doSearch` path that the trending chip rail uses).

On narrow screens (≤900px), the right rail collapses to a horizontal
chip row below the knowledge panel. Falls back to hidden on very
narrow.

Style: thin border, light grey background, similar weight to the
existing thumbnail decorations. Don't compete with the results.

### 8. Build pipeline integration

New scripts in `zettair-search/tools/` (or `zettair/wikipedia/`):

- `build_entity_set.py` — Wikidata dump → entity Q-set → docno
  classification. Output: `entity_classes.json` (per-docno
  entity-class label).
- `build_link_graph.py` — enwiki dump → entity-filtered edge list →
  CSR graph file.
- `build_related.py` — graph → random walks → `related.store` +
  `related.map`.

`setup.sh` invokes these in order at index-rebuild time, behind a
staleness check (same pattern as click_prior). If the index is
rebuilt, the graph and walks are rebuilt. If only top_titles changes,
nothing in this pipeline triggers.

Total prod-side CPU cost per rebuild: ~3-5 hours, single-threaded;
~1-2 hours with parallelism. Run once per ~monthly rebuild cycle.

---

## Milestones

### M1 — Entity classifier (~1 day)

- `build_entity_set.py` parses Wikidata, classifies our docnos.
- Writes `entity_classes.json` (~50 MB JSON; per-docno class label).
- Stats: ~30-50% of docnos pass.
- Unit-test against ~20 known cases (Mark_Carney → human;
  Liverpool_F.C. → organisation; London → place;
  Inception_(film) → work; The_Cuban_Missile_Crisis → event;
  Quantum_mechanics → none).

### M2 — Link graph (~1 day)

- `build_link_graph.py` streams the enwiki dump, extracts
  `[[Target]]` patterns from wikitext, filters by entity classification,
  writes the CSR graph.
- Stats: ~500-750k nodes, ~50-150M edges, ~600 MB-1 GB on disk.

### M3 — Random walks + storage (~1-2 days)

- `build_related.py` runs sampled walks per entity, aggregates, ranks,
  writes `related.store` + `related.map`.
- Parallelise via `multiprocessing.Pool` (embarrassingly parallel
  per-source).
- Tune K, L, α on a hand-graded set of ~30 well-known entities.

### M4 — Server + frontend (~1-2 days)

- `server.py` loads `related.map` at startup, projects per-result.
- `/search` response gains `related` field.
- `index.html` renders the right-rail panel, with mobile fallback.

### M5 — Quality tuning (~1-2 days)

- Hand-eval a few hundred queries. Iterate on K, L, α, top-N, score
  threshold.
- Add a small post-walk denylist for any garbage that slips through
  (year-disambig pages with the entity P31, oddly-classified things).

### M6 (deferred) — Personalised PageRank backend

If walk quality plateaus, swap the random-walk implementation for
PPR. Same output format. Same downstream. Same FlatStore. Pure
backend change.

### M7 (deferred) — Concept articles

Extend the graph to non-entity articles for topic-related-topic
links. Different ranking probably needed. Future PRD.

### M8 (deferred) — Related-search ranking signal

Use related-entities scores in the BM25 ranking path: slightly boost
results whose docno is related to entities found in the query. Could
be a meaningful ranking quality lift once the graph is built.

### M9 (deferred) — Dedicated `/related/<docno>` page

A standalone explorer that's nothing but a related-entity graph view.
Could be very compelling visually. Future PRD.

---

## Risks

- **Wikidata coverage / classification noise.** Some articles miss P31
  or have it wrong. Mitigation: hand-tune the class allowlist;
  classify ~30-50% as entities is plenty for an exploration feature.
  Mistakes erode quality slowly; not catastrophic.

- **Wikitext parsing edge cases.** Wiki markup is famously hairy
  (templates, transclusions, `{{redirect}}` shortcuts, piped links).
  Mitigation: a simple regex extractor catches ~95% of `[[Target]]`
  patterns; the remaining 5% is fine to lose at the graph-edge level
  since we have hundreds of edges per article.

- **Random-walk quality on sparse-link articles.** A new-ish entity
  article with few outlinks gets a short related list. Mitigation:
  drop entities below a minimum outdegree (~10) from the source set;
  let users fall through to no panel rather than a weak panel.

- **Graph stale by N weeks.** Built at index rebuild; misses anything
  newer. Mitigation: matches the corpus's freshness; if the rebuild
  cadence shortens, the graph follows.

- **Disk + RAM at scale.** 1 GB CSR + 300 MB FlatStore + 50 MB map in
  RAM is fine on the Hetzner box. If we ever grow the corpus beyond
  3M articles or expand to non-entities (~3-4× edges), we revisit.

- **The trash-neighbour problem.** Designed away by filtering at
  graph-construction time (entities only). The walks can only land
  on entities. Should not see disambig pages, list pages, etc.
  Unless Wikidata mis-classifies them, in which case M5 catches it
  with a denylist.

- **Right-rail layout on narrow screens.** The current results page is
  fluid-centred; adding a rail means a max-width breakpoint. Already
  handled in PRD-020's trending rail design pattern; reuse.

---

## Open questions

- **Wikidata dump vs Wikipedia API for entity classification.** Dump
  is offline and complete; API is live and rate-limited. Dump wins
  for v1 (rebuilds are infrequent).

- **Restart probability α.** 0.15 is the SimRank/PageRank default
  and the right starting point. Bing's setting from the original
  build isn't remembered; we'll sweep {0.05, 0.10, 0.15, 0.30} in
  M5 against a hand-graded set and pick.

- **Should we surface relatedness scores in the panel?** Probably no
  — score numbers feel academic. Just show the ordered list.

- **What about the knowledge panel?** When the top result has a
  knowledge panel, the right rail sits alongside it. Both visible
  at once. Could feel busy. Worth a design pass when M4 lands.

- **Per-class biases.** Should walks weight "same-class" neighbours
  higher (so people-articles surface more people, not 50% places)?
  Maybe. Default in v1: no class biasing. Tune in M5 if results look
  weird.

- **Symmetric vs asymmetric relatedness.** Resolved: directional.
  The original Bing version was directional and the asymmetric
  shape is the right signal — A→B captures "from A, you reach B"
  which is the natural query intent (clicking from a specific
  entity toward broader/related ones differs from the reverse).
  Forward walks only; no symmetric merge.

- **Number of items in the rail.** 6, 8, 10? Default 8. M5 tune.
