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
final_weight = bm25_weight × (1.0 + α × log(1.0 + click_prior[docno]))
```

Where:
- `bm25_weight` — Zettair's existing accumulated BM25 score (sum of term contributions)
- `click_prior[docno]` — decayed click score for this document (float, ≥ 0)
- `α` — tuning parameter, default **0.3**
- `log` — natural log, dampens the effect so blockbusters don't dominate

The multiplication happens in the **`post()` function** in `okapi.c`, after all term weights have been accumulated for each document. This is the cleanest injection point — it touches the final score, not the per-term accumulation.

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

**Two changes only:**

**a) Global prior array (top of file, after includes):**
```c
/* Click prior — loaded from click_prior.bin at startup */
static float *g_click_prior = NULL;
static unsigned int g_click_prior_len = 0;
static double g_click_alpha = 0.3;

void okapi_load_prior(const char *path, double alpha) {
    FILE *f = fopen(path, "rb");
    if (!f) return;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    g_click_prior_len = sz / sizeof(float);
    g_click_prior = malloc(sz);
    if (g_click_prior) fread(g_click_prior, sizeof(float), g_click_prior_len, f);
    fclose(f);
    g_click_alpha = alpha;
}
```

**b) `post()` function — multiply accumulated weight by prior:**
```c
static enum search_ret post(struct index *idx, struct query *query,
  struct search_acc_cons *acc, int opts, struct index_search_opt *opt) {

    while (acc) {
        assert(acc->acc.docno < docmap_entries(idx->map));

        /* Apply click prior if loaded */
        if (g_click_prior && acc->acc.docno < g_click_prior_len) {
            float prior = g_click_prior[acc->acc.docno];
            acc->acc.weight *= (1.0 + g_click_alpha * log(1.0 + prior));
        }

        acc = acc->next;
    }
    return SEARCH_OK;
}
```

**c) `okapi_load_prior()` called from `main.c` / `zet.c`** at startup, before any queries, passing path via env var `ZET_CLICK_PRIOR`.

### 3. Hot-swap

`click_prior.bin` is loaded once at startup into a malloc'd array. When new clickstream data arrives (monthly), the refresh script:
1. Rebuilds `click_prior.bin`
2. Sends `SIGUSR1` to the `zet` process
3. Signal handler calls `okapi_load_prior()` again (free old array, malloc new one)

For our use case (subprocess-per-query model in `server.py`), hot-swap isn't needed — each `zet` invocation is a fresh process. Just rebuild the file and it's picked up automatically.

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
