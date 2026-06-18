# PRD-033: Front-of-Search Cache, Backend Routing, and Sharding

**Status:** Draft
**Author:** metabot (with the user, 2026-06-18 conversation)
**Date:** 2026-06-18

---

## Summary

A three-stage scaling roadmap. We are at ~16 req/s today; the corpus
is immutable for ~quarterly windows, which is a much friendlier
caching environment than most search engines have. Each stage is
independently shippable and each unlocks the next:

1. **Front-of-search cache** — in-process LRU on `/search`, invalidated
   only on corpus rebuild. Lifts the QPS ceiling ~3× and drops p50
   dramatically.
2. **Backend routing** — web/cache layer that points at one of N
   live backends. Enables zero-downtime corpus rebuilds (rebuild B
   while A serves; flip pointer when B passes a health check).
3. **Replication and/or sharding** — N backends fronted by the same
   web/cache layer. Replication scales QPS linearly; document
   sharding scales corpus size linearly.

The corpus's immutability between rebuilds is the load-bearing
property that makes all three of these dramatically simpler than
they would be in a "fresh index" search engine. Spending care on
the cache invalidation story (stage 1) gives us the right primitive
for stages 2 and 3.

---

## Motivation

**Where we are.** A 60-second loadtest on prod (2026-06-18, run from
the box at concurrency=10, hitting `localhost:8765`) measured:

- Throughput: **15.5 req/s**
- p50 528 ms · p95 1525 ms · p99 2082 ms
- 0 errors over 940 requests

86% of requests fall in the 300–830 ms range — the warm zet-worker
case. A long tail extends to ~3.2 s for queries that hit GC, the
summariser on long docs, or worker-queue waits. The README's
historical "~18 req/s" baseline checks out.

The 170 QPS the user remembered was from a different system
(probably Bing-era infrastructure); not relevant here.

**The constraint that makes caching easy.** The corpus is rebuilt
~quarterly. Between rebuilds, `/search?q=...` for the same `(q, n)`
returns deterministically identical body content. The only
volatile fields in a `/search` response are:

- `summary` and `event_date` (PRD-018 / 021 — driven by the
  trending payload, which updates hourly; news summaries refresh
  via the Mac Mini pipeline on top of that).
- `related` (PRD-025 — the related-entity list itself is immutable,
  but the rail header text varies with `top_docno`'s entity class).
- `phases` / `enrich` / `took_ms` (telemetry, not user-visible
  semantically — fine to cache or to overwrite per-request).

Everything else (`results[]`, snippets, titles, thumbs, URLs,
reading-time, difficulty) is bit-identical between rebuilds. The
cache split is "result list and per-result enrichment = cacheable;
summary, event_date, related = recompute on every request."

**Where we want to go.** The user articulated three goals, in
roughly increasing engineering cost:

1. Handle traffic spikes (Reddit / iOS launch / news event)
   without falling over.
2. Rebuild the corpus without downtime.
3. Eventually have "as much Wikipedia as I god damn feel like" —
   corpus larger than what fits in one machine's RAM/disk budget.

---

## Stage 1: Front-of-Search Cache

### Hit-rate expectation

The query distribution is Zipfian. Bing's historical hit rate at
this layer was ~70%. With 44k known queries weighted by clickstream
popularity and a cache of even a few thousand entries, the head
queries repeat enough that 60–80% is a reasonable target. We will
**measure**, not assume.

### Throughput math (against today's 15.5 req/s baseline)

Assuming cache hits cost ~1 ms (in-process LRU + JSON serialise)
and misses cost the baseline 641 ms mean:

| Hit rate | Mean latency | Sustained req/s | Ceiling vs today |
|---|---|---|---|
| 0% (today)  | 641 ms |  ~15.5 | 1.0× |
| 30%         | ~451 ms |  ~22 | 1.4× |
| 50%         | ~322 ms |  ~31 | 2.0× |
| 70%         | ~193 ms |  ~52 | 3.3× |
| 80%         | ~129 ms |  ~78 | 5.0× |
| 90%         |  ~65 ms | ~155 | 10× |

The sustained calculation pegs the ceiling against the 4 zet
workers handling the miss traffic at ~500 ms median. Real-world
will land a bit lower (concurrency isn't infinite; hits aren't
quite free) but the shape holds.

### Tail-latency effect

Cache helps mean and median dramatically — most users see ~1 ms
for the head queries. p95 and p99 do not improve, because the slow
tail comes from misses. The UX win is "median feels instant; the
1% slowest are unchanged from today" — which is the correct
direction.

### Cache key

`(query_norm(q), n)`. We already normalise `q` for the summaries
lookup; using the same normalisation here gives free deduping of
casing / whitespace variants.

### Cache value

The full `/search` response **minus** the volatile fields:

```
cache.value = {
    results: [...],          # immutable until rebuild
    total, took_ms, phases,  # leave: telemetry, harmless if stale
    enrich,                  # leave: telemetry
    # summary, summary_kind, summary_source, event_date: NOT cached
    # related: NOT cached (depends on trending state for header)
}
```

On a hit, the server overlays a fresh `summary` / `summary_source` /
`event_date` / `related` block computed via the existing code paths
(both are cheap FlatStore lookups + dict traversal).

### Cache invalidation

Three triggers:

1. **Corpus rebuild** (the big one). `setup.sh` already writes
   `wikiindex/index.param.0` once per rebuild. Stat its mtime at
   each request; if it's newer than the cache-build time, drop
   the whole cache. ~Microseconds per request to stat.
2. **`click_prior.bin` mtime change**. Click prior recomputes
   monthly when new clickstream lands; results-order can shift.
   Same mtime-stat mechanism.
3. **Manual flush.** `POST /admin/cache/flush` with a shared
   secret env var, for ops. (Bearer token; not Caddy-fronted with
   auth.)

We do NOT invalidate on every trending fetch or news-summary
install — those affect the volatile fields, which are recomputed
on every request anyway.

### Implementation

In-process Python `OrderedDict` LRU in `server.py`. Single uvicorn
process today, so cache is shared across requests but not across
processes. If we go multi-process later, that's a Redis port (see
Stage 1.5).

```python
from collections import OrderedDict
import threading

_search_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()
_cache_max = int(os.environ.get("ZET_CACHE_SIZE", "5000"))
_cache_corpus_mtime: float = 0.0   # mtime of index.param.0 when cache filled

# inside /search handler:
key = (qn, n)
with _cache_lock:
    hit = _search_cache.get(key)
    if hit is not None:
        _search_cache.move_to_end(key)
# ... fall through to zet on miss; cache the result minus volatile fields ...
```

Background mtime watcher (or just stat-on-access) drops the cache
when `index.param.0` changes.

### Memory cost

5000 entries × ~5 KB each (results array + snippets + thumbs) ≈
~25 MB. Trivial against the box's 8 GB.

### Env knobs

- `ZET_CACHE_SIZE` — max entries (0 disables, default 5000).
- `ZET_CACHE_TTL_S` — optional safety TTL on individual entries
  (default 86400 = 24h; not strictly needed because of mtime
  invalidation, but cheap belt-and-braces).
- `ZET_CACHE_FLUSH_TOKEN` — shared secret for the manual flush
  endpoint; if unset, the endpoint 404s.

### Instrumentation

New endpoint `/health/cache`:

```json
{
  "size": 3241,
  "max": 5000,
  "hits_total": 18420,
  "misses_total": 5331,
  "hit_rate": 0.776,
  "corpus_mtime": "2026-06-05T08:34:11Z",
  "ttl_s": 86400
}
```

Counters reset on flush or restart. After 24h of prod traffic we
know the real hit rate.

### Cost / time

~1 day to implement, instrument, deploy. ~1 week to gather hit-rate
data and decide if Stage 1.5 (Redis) is worth it.

### Risks

- **Cache poisoning via `n` parameter.** A bad client iterating `n=1..50`
  burns 50 cache entries per query. Cap or quantize `n` to a small
  set of values (e.g. {1, 5, 10, 20, 50}).
- **Volatile-field staleness if we miscategorise a field.** Audit
  `enrich_results` before deciding which fields enter the cached
  payload.
- **Cache hit before the server has loaded.** The startup lifespan
  loads sidecars; cache initialises empty. Naturally safe.
- **Click-prior invalidation lag.** If click_prior is updated but
  `index.param.0` is not, results order changes but the cache
  doesn't notice. Add the click-prior mtime as a second invalidation
  trigger.

### Acceptance

Ships when:

- `/health/cache` returns sensible counts after a 60s loadtest.
- Loadtest at concurrency=10 against the same fixed query pool
  reports throughput ≥30 req/s.
- p50 for the cache-hit case is ≤10 ms (overlay cost only).
- A `POST /admin/cache/flush` empties the cache and the next
  request misses + repopulates.

---

## Stage 1.5: Cache promotion to Redis (optional)

Skip if Stage 1 hit rate or QPS gains are already enough. Add
this only when one of these triggers:

- We move to multiple uvicorn processes (separate caches per
  process means lower aggregate hit rate; Redis becomes a shared
  cache).
- Stage 2 (backend routing) needs the cache visible to multiple
  backends.
- We want hit-rate persistence across server restarts.

Redis sits on the same box; latency hop is ~0.3 ms (acceptable
relative to the ~1 ms in-process lookup). API surface in server.py
is unchanged (`_search_cache.get / set` is wrapped behind an
interface).

---

## Stage 2: Backend Routing

### Goal

Zero-downtime corpus rebuilds. Today a rebuild takes 6–8 h and the
service either stays on the old index (silently stale) or goes
down (incident).

### Architecture

```
                            ┌─ backend A (live, index v1)
Caddy ─▶ web/cache layer ──▶┤
                            └─ backend B (rebuilding, then live)
```

The "web/cache layer" is the existing FastAPI server in `server.py`
with three new responsibilities:

1. The Stage 1 cache (shared across backends if Redis-backed).
2. A `BACKEND_URLS` env list (e.g. `localhost:8766,localhost:8767`)
   and a `current_backend` pointer.
3. A health probe per backend (canonical-queries smoke test).

The `zet` worker pool moves from being co-located in the FastAPI
process to being a separate `zettair-backend.service` on a port,
one per backend. Each backend points at its own index prefix.

### File layout on the volume

Two backend "slots" share the volume. Each has its own
sub-directory of sidecars and own index:

```
/mnt/wikipedia-source/
  backend-a/
    wikiindex/
    enwiki_top1m.{docstore,docmap,reading.bin,...}
    summaries.{store,map}      (or shared — see below)
    related.{store,map}
    ...
  backend-b/
    (mirror of backend-a)
  trending/current.json         (shared)
  summaries/                    (shared queue dirs)
```

**Shared vs backend-local stores.** Decision per store:

| Store | Shared | Per-backend | Why |
|---|---|---|---|
| Index, field sidecars | | ✓ | Tied to corpus |
| Docstore, docmap | | ✓ | Tied to corpus |
| Titles sidecar | | ✓ | Tied to corpus |
| URLs store | | ✓ | Tied to corpus |
| Images store | | ✓ | Tied to corpus |
| Reading sidecar | | ✓ | Tied to corpus |
| Related store + class | | ✓ | Tied to corpus |
| Snippets store | | ✓ | Tied to corpus |
| `summaries.{store,map}` | ✓ | | Query→summary mapping; doesn't depend on corpus |
| `trending/current.json` | ✓ | | Rail data; recomputed hourly |
| Summary queue dirs | ✓ | | Mac Mini pipeline |
| Click prior | | ✓ | Indexed by docid which differs across backends |

**Disk cost.** ~25 GB per-backend × 2 backends + shared = ~55 GB.
The 80 GB Hetzner volume covers it with margin; a larger corpus
(say 3M) would need a bigger volume.

### Rebuild flow

```
1. Operator: triggers rebuild on the inactive backend (B).
   setup.sh runs with BACKEND_PREFIX=backend-b. ~6-8 h.
2. setup.sh terminates with B's new index in place.
3. Backend B's zet worker pool restarts pointing at the new
   sidecars. Loads in ~10s.
4. Web/cache layer probes B's health: runs N canonical queries
   ("einstein", "morrissey", "iraq", ...) and checks for sane
   rank-1 docnos. (Reuses the existing `tests/test_setup.sh`
   query bank, ideally.)
5. If health passes: flip the pointer, flush the cache.
6. Backend A is now inactive — available for the next rebuild.
```

### Atomicity

Pointer flip is in-memory in the web/cache layer. There's no
"two-phase commit" needed because:

- In-flight requests on A finish on A — that's fine; results are
  consistent with the cache state at the moment the request hit.
- New requests after the flip route to B; cache is flushed so they
  get B's (fresh) results.

### Health-check definition

The web/cache layer should NOT route to a backend that's loading,
mid-restart, or returning garbage. A minimum probe per backend:

- `GET /search?q=einstein&n=1` returns 200 in <5s.
- Top result's docno matches a known-good set for that query.
- `total` is plausible (not 0 — index isn't half-loaded).

Probe every 10s on each backend; flip the pointer to the healthy
one if the current backend fails 3 probes in a row.

### Risks

- **Sidecar coherence across backends.** When summaries.store is
  shared but `:news` summaries are docno-keyed (PRD-018's queue
  uses query_norm so OK; PRD-025 related is keyed by docno which
  IS tied to corpus version). If the related-store on disk doesn't
  match B's index, related rail breaks. The "Rebuild related-store
  with safe_id docnos" entry in PRD-032 is part of this story.
- **`click_prior.bin` keying.** Indexed by Zettair docid which
  shifts every reindex. The new index needs its own click_prior
  rebuilt as part of B's pipeline (setup.sh already does this).
- **trending mtime sync.** `current.json` is updated hourly by the
  shared trending fetcher. Both backends see it; that's correct.
- **Mac Mini pipeline.** Drains queue dirs into `summaries.store`
  which is shared. No per-backend coupling.
- **Caddy in front of FastAPI.** Caddy still talks to one
  FastAPI port (the web/cache layer). No Caddy changes needed.

### Cost / time

~3-5 days to design, implement, test offline. Plus a real corpus
rebuild against it as the validation.

---

## Stage 3: Replication

### Goal

Linearly scale QPS by running N copies of the same backend behind
the web/cache layer.

### Architecture

Stage 2 already gives us "N backends, pick one." Stage 3 is the
same code, configured to load-balance across all healthy backends
rather than route to a single `current`.

### When to do it

Only when our QPS is genuinely the bottleneck — i.e. after Stage 1
(cache) and Stage 2 (no-downtime rebuild) have shipped and the
prod loadtest still tops out below acceptable headroom for
foreseeable traffic.

For context: with Stage 1's cache at 70% hit rate, the practical
ceiling becomes ~50 req/s. A trending event (Reddit, iOS launch)
might burst to 200 req/s for a few minutes. That's the regime
where replication starts mattering. Below that, it's premature.

### Cost

Mostly hardware. A second VPS clone plus a routing rule. Software
complexity is minimal — backend selection becomes round-robin or
least-loaded across healthy backends.

---

## Stage 4: Document Sharding

### Goal

Corpora larger than what fits on one machine. The user's
"as much Wikipedia as I god damn feel like" — say, the full ~7M
English Wikipedia articles, or multi-language corpora.

### Architecture

```
                              ┌─ shard 1 (docids 0..N/k)
web/cache layer ──▶ fanout ──▶┤  shard 2 (docids N/k..2N/k)
                              └─ ...
                                ▼
                              merge + rerank
                                ▼
                              return top-N
```

### Major challenges

1. **Shard assignment.** Hash-by-docno is simplest. Smarter is
   shard-by-entity-class so the related rail can stay in-shard for
   common queries, but that's an optimisation, not a v1.

2. **Score merging across shards.** Zettair's BM25 (and our BM25F)
   uses corpus-wide IDF. If each shard computes its own per-shard
   IDF, the merged top-N is mildly wrong (the same term has
   different "rarity" scores across shards). Two options:
   - Accept the skew. Top-1 and top-5 are usually fine; the tail
     can be wonky. Probably tolerable for our use case.
   - Run an IDF-sync step that computes global IDF once per
     rebuild and pushes per-term DFs to each shard. ~1 day of
     work; gives clean scores.

3. **Per-field BM25F across shards.** Same IDF problem but
   per-field. Same fix.

4. **Sidecars.** Each shard owns its own slice of:
   - Index, docstore, snippets, titles, URLs, images, reading,
     related, click_prior, field_lengths, field_stats.

   The web/cache layer needs to know which shard owns which docno.
   Maintain a (docno → shard) map (small, ~bytes per entry).

5. **Summary store.** Stays shared. Keyed by query_norm, not
   docno. No sharding needed.

6. **Trending.** Stays shared.

### When to do it

Only when corpus size is the bottleneck. We are nowhere near
that. Capture the design here; don't build until there's a
forcing function.

### Hybrid (Replication × Sharding)

The big-search-engine architecture: M replicas of K shards. The
web/cache layer fans out to one replica per shard, merges, returns.
Wholly overkill for now. Captured for completeness.

---

## Decision: what to ship now

Recommended sequence:

| Stage | Effort | Value | Ship when |
|---|---|---|---|
| Stage 1 — In-process LRU cache | ~1 day | High (3× QPS ceiling, dramatically better p50) | Next |
| Stage 2 — Backend routing | ~3-5 days | High (zero-downtime rebuilds) | After Stage 1 has stable hit-rate data |
| Stage 1.5 — Redis | ~1 day | Conditional on Stage 2 or multi-process | Only if Stage 2 motivates it |
| Stage 3 — Replication | ~1 day (after Stage 2) | Conditional on real QPS pain | Only if traffic outgrows Stage 1 |
| Stage 4 — Sharding | ~weeks | Conditional on corpus growth | Only when one machine isn't enough |

---

## Non-goals

- **Edge / CDN caching.** Caddy can be configured to honour
  `Cache-Control` headers but that's an orthogonal lever; we get
  most of the win in-process. CDN comes later if at all.
- **Stale-while-revalidate.** The cache is either fresh-against-
  corpus or invalidated; there's no in-between.
- **Per-user personalisation.** Same cache key for everyone.
- **Search-suggestion caching.** `/suggest` is already
  ~milliseconds via in-memory binary search; nothing to cache.
- **Replacing FastAPI.** The web/cache layer IS FastAPI; we don't
  rewrite it in Go.

---

## Open questions

1. **Cache hit rate in practice.** Estimated 60-80%. We have to
   measure. Real hit rate determines whether Stages 1.5 and 3 are
   worth chasing.

2. **Backend-routing UX during a rebuild.** Does the operator
   explicitly trigger "rebuild B", or does a timer? Probably
   manual, mirroring the current corpus-rebuild discipline (the
   2026-06-09 conversation about manual quarterly rebuilds).

3. **What happens to in-flight `:news` summaries when we flip
   backends?** The `summaries.store` is shared, so an in-flight
   summary that hasn't installed yet completes against the
   already-shared store. The web/cache layer never knows.

4. **Does Stage 2 obviate the need for `systemd-run --unit=rebuild`?**
   Once we can rebuild B while A serves, the user-facing surface
   for "rebuild the corpus" becomes a one-liner with no downtime
   risk. The systemd-run pattern stays useful as a fallback for
   single-backend prod (which is what we have today).

5. **iOS app stability across stages.** All response shapes stay
   additive; no field renames. PRD-028 stays stable.

---

## References

- PRD-007 — persistent worker pool (the unit we're putting behind a
  cache).
- PRD-012 — corpus selection (`select_top_articles.py` unions
  trending history; this drives the quarterly rebuild cadence).
- PRD-019 — BM25F (the per-field weights that need to stay
  consistent across replicated/sharded backends).
- PRD-020 — trending (the cache-invalidation question — trending
  state changes hourly).
- PRD-032 — unbuilt features. Add a row for this PRD; cross-link
  from the rebuild-related-store and session-logging entries that
  intersect with sharding.

---

## Loadtest data (2026-06-18 baseline)

For future-readers comparing against this stage. Run on the prod
box (`http://localhost:8765` to avoid TLS), concurrency 10,
duration 60s, query pool ~44k autosuggest-weighted:

```
Throughput     : 15.5 req/s
mean           : 640.9 ms
p50            : 527.8 ms
p75            : 666.0 ms
p90            : 960.5 ms
p95            : 1525.1 ms
p99            : 2082.1 ms
max            : 3191.0 ms
0 errors over 940 requests
```

Re-run the same command after Stage 1 to validate the throughput
lift.
