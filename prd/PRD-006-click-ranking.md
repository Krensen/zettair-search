# PRD-006: Click-Prior Ranking (BM25 × Popularity)

**Status:** Approved  
**Priority:** High  
**Complexity:** High (C patch + data pipeline)

---

## Problem

Zettair's BM25 ranking is purely textual — it has no notion of document popularity. A query like "einstein" ranks obscure articles mentioning the word "einstein" equally to the main Albert_Einstein article if their term statistics happen to match. We have 15 months of Wikipedia clickstream data showing real-world article popularity. We should use it.

---

## Goal

Bake a per-document click prior directly into Zettair's BM25 scoring function, so popular articles get a boost *inside* the ranker — not as a post-processing step applied to a truncated result list.

---

## Why In-Ranker (Not Post-Ranking)

Post-ranking re-scores the top-N results returned by Zettair. The fatal flaw: if the correct result ranks #51 in pure BM25 but would rank #1 after click boosting, we never see it. With Zettair's threshold pruning this is a real risk for navigational queries.

In-ranker means the click prior competes fairly against BM25 term weights from the very first accumulator comparison.

---

## Scoring Formula

```
score += r_dt × w_t × r_qt × click_boost(docno)
```

Where:
- `r_dt` — BM25 TF component (term frequency normalised by doc length)
- `w_t` — IDF weight (inverse document frequency)  
- `r_qt` — query term frequency normalisation
- `click_boost(docno)` = `1.0 + α × log(1.0 + click_prior[docno])`

This mirrors exactly how IDF works — a **per-term, inline, multiplicative** factor applied on every term contribution. The click prior scales every BM25 term contribution for that document, so:

- A document with high clicks AND good term matches gets boosted on every term accumulation
- Documents with zero clicks return `click_boost = 1.0` — identical to current behaviour
- Threshold pruning sees the boosted scores from the very first accumulator comparison, so popular documents survive pruning that pure BM25 would have discarded
- α controls the strength: 0.0 = pure BM25, 0.3 = light boost, 1.0+ = popularity dominates

---

## Implementation

### 1. Data pipeline: `build_click_prior.py`

Reads all clickstream files, applies decay, produces `click_prior.bin`:

```
Format: binary file, array of float32
Index:  Zettair internal docno (0-indexed, sequential)
Value:  decayed click score (0.0 if no clicks)
Size:   256,534 × 4 bytes = ~1MB
```

**Docno → title mapping:** Zettair's `docmap` stores TREC docno (= article title) alongside internal docno. We need a mapping file `docno_map.tsv` (internal_docno → title) built by querying the index at startup.

**Build steps:**
1. Walk the index docmap to produce `docno_map.tsv` (title → internal_id)
2. Aggregate 15-month clickstream with decay (same as autosuggest pipeline)  
3. Join on title → output `click_prior.bin`

### 2. C patch: `okapi.c`

**Three additions only:**

**a) Global prior array + inline helper (top of file, after includes):**
```c
#include <math.h>

/* Click prior — loaded from click_prior.bin at startup */
static float *g_click_prior = NULL;
static unsigned int g_click_prior_len = 0;
static double g_click_alpha = 0.3;

static inline double click_boost(unsigned long int docno) {
    if (g_click_prior && docno < g_click_prior_len && g_click_prior[docno] > 0.0f)
        return 1.0 + g_click_alpha * log(1.0 + (double)g_click_prior[docno]);
    return 1.0;
}

void okapi_load_prior(const char *path, double alpha) {
    FILE *f = fopen(path, "rb");
    if (!f) return;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    free(g_click_prior);  /* safe to call on NULL */
    g_click_prior_len = sz / sizeof(float);
    g_click_prior = malloc(sz);
    if (g_click_prior) fread(g_click_prior, sizeof(float), g_click_prior_len, f);
    fclose(f);
    g_click_alpha = alpha;
}
```

**b) `METRIC_PER_DOC` — six occurrences, same one-line change each:**

Every `METRIC_PER_DOC` block in `or_decode`, `or_decode_offsets`, `and_decode`, `and_decode_offsets`, `thresh_decode`, `thresh_decode_offsets` contains:

```c
/* BEFORE */
(acc->acc.weight) += r_dt * w_t * r_qt;

/* AFTER */
(acc->acc.weight) += r_dt * w_t * r_qt * click_boost(acc->acc.docno);
```

That's the entire patch to the scoring logic — one inline function call added to each of six identical lines.

**c) `okapi_load_prior()` called from `zet.c`** at startup, before any queries:
```c
/* In zet.c main(), after index is opened: */
const char *prior_path = getenv("ZET_CLICK_PRIOR");
double alpha = getenv("ZET_CLICK_ALPHA") ? atof(getenv("ZET_CLICK_ALPHA")) : 0.3;
if (prior_path) okapi_load_prior(prior_path, alpha);
```

### 3. Hot-swap

For our subprocess-per-query model in `server.py`, hot-swap isn't needed — each `zet` invocation is a fresh process that loads `click_prior.bin` at startup. When new clickstream data arrives monthly, the refresh script rebuilds `click_prior.bin` and it's picked up automatically on the next query.

### 4. `docno_map.tsv` — mapping Zettair internal IDs to titles

Need a script `build_docno_map.py` that runs:
```bash
echo "__DOCNO_DUMP__" | ./zet -f index --summary=plain -n 999999
```
Actually Zettair doesn't expose this directly. Better approach: parse `index.map.0` binary directly, or use the TREC docno output from a wildcard query. 

**Simpler approach:** During indexing, Zettair assigns docnos sequentially in the order documents appear in the TREC file. So docno 0 = first article in `simplewiki.trec`, docno 1 = second, etc. We can recover the mapping by re-reading `simplewiki.trec` and extracting `<DOCNO>` tags in order.

---

## Files Changed

| File | Change |
|------|--------|
| `zettair/devel/src/okapi.c` | Add `g_click_prior` global + `okapi_load_prior()` + patch `post()` |
| `zettair/devel/src/okapi.h` or `zettair.h` | Declare `okapi_load_prior()` |
| `zettair/devel/src/zet.c` (or main entry) | Call `okapi_load_prior()` at startup if env var set |
| `zettair/wikipedia/build_docno_map.py` | Extract docno → title mapping from TREC file |
| `zettair/wikipedia/build_click_prior.py` | Aggregate clickstream → `click_prior.bin` |
| `zettair/wikipedia/click_prior.bin` | Generated binary (gitignored) |
| `zettair/wikipedia/docno_map.tsv` | Generated mapping (gitignored) |

---

## Tuning Parameter α

Start at **α = 0.3**. To evaluate:

- α = 0.0 → pure BM25 (baseline)
- α = 0.3 → light popularity boost (recommended start)
- α = 1.0 → strong popularity signal
- α = 3.0 → popularity dominates (basically sorts by clicks, BM25 is tiebreaker)

---

## Test Plan

### Unit tests (before touching the live index)

**T1 — Prior loads correctly**
- Build a tiny `click_prior.bin` with known values (5 docs, known floats)
- Call `okapi_load_prior()`, verify `g_click_prior[i]` matches expected values
- Verify `g_click_prior_len` == 5

**T2 — Scoring formula is correct**
- With α=0.3 and prior=1000.0: multiplier = 1 + 0.3×log(1001) ≈ 1 + 0.3×6.91 ≈ 3.07
- With α=0.3 and prior=0.0: multiplier = 1.0 (no change)
- With prior=0.0 for all docs: results must be identical to baseline BM25

**T3 — Zero prior = no change**
- Build `click_prior.bin` of all zeros
- Run a query, compare scores to baseline — must be identical

### Integration tests (against live index)

**T4 — Navigational query: "einstein"**
- Baseline: check what rank `Albert_Einstein` gets
- With prior: `Albert_Einstein` should rank #1 (it has millions of clicks)
- If it was already #1, verify its score increased by the expected multiplier

**T5 — Navigational query: "paris"**
- `Paris` (city) should rank above `Paris_(mythology)`, `Paris_Hilton` etc
- Clickstream strongly favours the city article

**T6 — Specific query: "einsteinium element"**
- `Einsteinium` should still rank above `Albert_Einstein` for this query
- BM25 term match on "einsteinium" should dominate the click prior
- Verifies α=0.3 doesn't over-boost popular articles on specific queries

**T7 — docno_map integrity**
- Verify `docno_map.tsv` has exactly 256,534 entries
- Spot-check: `Albert_Einstein` maps to a valid internal docno
- Verify no duplicate docnos

**T8 — click_prior.bin integrity**
- Verify file size = 256,534 × 4 bytes = 1,026,136 bytes
- Verify `Albert_Einstein` entry > 0
- Verify at least 50% of entries are > 0 (we have clicks for ~158k articles)
- Verify no NaN or Inf values

**T9 — α sensitivity**
- Run "einstein" query with α ∈ {0.0, 0.1, 0.3, 1.0, 3.0}
- Document rank of `Albert_Einstein` at each α
- Pick the α where specific queries (T6) still work correctly

**T10 — Performance regression**
- Run 20 queries with and without prior
- `post()` should add < 1ms overhead (it's a single pass over accumulators)
- Verify no memory leaks (valgrind on small index if needed)

### Regression tests

**T11 — Existing test suite**
- Run `make test` in `zettair/devel/` — all existing tests must pass
- The patch only touches `post()` and adds a new function — should be safe

---

## Acceptance Criteria

- [ ] `Albert_Einstein` ranks #1 for query "einstein" (currently doesn't)
- [ ] `Paris` ranks #1 for query "paris"  
- [ ] Specific queries (T6) not broken by over-boosting
- [ ] `make test` passes
- [ ] No performance regression > 2ms per query
- [ ] α is configurable via env var `ZET_CLICK_ALPHA`

---

## Rollback

`git checkout checkpoint-3` in `zettair` repo. Remove `ZET_CLICK_PRIOR` env var and restart server. The prior is purely additive — removing the env var reverts to pure BM25 with zero code changes needed.

---

## On the clicks file

Yes — `click_prior.bin` is built from the same aggregated clickstream data used for autosuggest. The `build_click_prior.py` script shares the decay logic with `build_autosuggest.py` but outputs a float array indexed by docno rather than a sorted query list. We need `docno_map.tsv` as the join key. Building that is the first step.
