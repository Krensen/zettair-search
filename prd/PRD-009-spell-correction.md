# PRD-009: Spell Correction

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-03-31

---

## Problem

Users who mistype queries get poor results or zero results with no guidance. We have no mechanism to detect or recover from spelling errors. The search engine should behave like a good librarian: if it can't find what you asked for, suggest what you probably meant.

---

## Goal

Show a "Did you mean: *query*" suggestion above results when the user's query appears to contain a spelling error, linking to the corrected search. Trigger conservatively — only when results are sparse — to avoid false positives on correctly-spelled rare or technical terms.

---

## Non-Goals

- Real-time correction while typing (that's autosuggest's job)
- Correcting multi-word phrases as a unit (word-by-word is sufficient)
- Neural/embedding-based correction
- Building a confusion matrix from query logs (deferred until log volume is sufficient)

---

## Approach

**SymSpell-style symmetric delete + autosuggest vocabulary as prior.**

### Why SymSpell over a BK-tree or naive edit distance

Naive edit distance against 152k vocabulary entries at query time is ~50ms. SymSpell pre-generates all deletes at index distance ≤ 2 at build time, so lookup is O(1) amortised — under 1ms regardless of vocabulary size.

The key insight: for edit distance ≤ 2, every candidate correction differs from the query by at most 2 deletions *plus* 2 insertions. By pre-storing all single and double deletions of every vocabulary word, we can find all candidates by generating deletions of the *query* and looking them up in a hash table.

### Vocabulary and prior

We use the **autosuggest vocabulary** (152k entries with click counts) as both the candidate set and the ranking prior. This is appropriate because:
- It contains correctly-spelled Wikipedia article titles and common queries
- Entries are ranked by real click frequency — popular (correctly-spelled) queries rank above obscure ones
- It's already loaded in memory at server startup

### Trigger condition

Only suggest a correction when:
1. Result count < 5, **or**
2. Top result score is below a threshold suggesting weak match (TBD via tuning)

This prevents false positives on correctly-spelled rare terms ("eigenvalue", "Dzerzhinsky") that happen to be low-frequency.

---

## Architecture

### New file: `spellcheck.py`

Runs as a persistent subprocess (same pattern as `summarise.py`), or alternatively as an in-process module loaded at startup — since it's pure Python with no blocking I/O, in-process is simpler.

**Build phase** (`build_symspell()` — runs at startup, ~1 second):
1. Load `autosuggest.json` (already available)
2. For each `(query, count)` entry, generate all single-delete variants
3. Store in a dict: `delete_variant → [(original_word, count), ...]`
4. Also store double-delete variants for distance-2 coverage

**Query phase** (`correct(query_terms, result_count)` — called per search):
1. If result count ≥ 5: return None (no correction needed)
2. For each query term:
   a. Generate all single and double deletes of the term
   b. Look up each in the delete dict → candidate set
   c. For each candidate, compute actual edit distance (Levenshtein) to confirm ≤ 2
   d. Rank candidates by: (edit_distance ASC, click_count DESC)
   e. If best candidate ≠ original term and edit_distance ≥ 1: flag this term for correction
3. Build corrected query string from flagged terms
4. Return corrected query, or None if no correction found

### Changes to `server.py`

Load `SpellChecker` at startup (after autosuggest loads, since it shares the data).

In `/search` endpoint, after getting results:
```python
correction = None
if parsed["total"] < 5:
    correction = spell.correct(q.strip())
```

Add `"correction": correction` to the JSON response (null if no suggestion).

### Changes to `index.html`

When `data.correction` is non-null, render above the results:

```
Did you mean: london eye height    ← linked to corrected search
```

Styled to match Google's "Did you mean" — italic, slightly smaller than results meta, correction text in blue link.

---

## Ranking correction candidates

For a given misspelled term, we may have multiple candidates at the same edit distance. Ranking:

1. **Edit distance** (lower = better) — distance 1 always beats distance 2
2. **Click count** (higher = better) — among equal-distance candidates, prefer the one users actually click
3. **Term length** — prefer corrections of similar length (avoids "blak" → "black" being beaten by "la")

---

## Example behaviour

| Query | Total results | Correction shown |
|-------|--------------|-----------------|
| `londno` | 0 | "Did you mean: **london**" |
| `einsten relativity` | 2 | "Did you mean: **einstein** relativity" |
| `blakc hole` | 1 | "Did you mean: **black** hole" |
| `eigenvalue` | 12 | *(no correction — enough results)* |
| `london` | 10,102 | *(no correction)* |
| `Dzerzhinsky` | 3 | *(no correction — correct spelling, just rare)* |

The last case is the hard one. Edit distance will find "Dzerzhinsky" has no close autosuggest match, so no correction is offered — correct behaviour.

---

## Implementation Phases

### Phase 1 — Core spell checker
- Write `spellcheck.py`: `build_symspell()` + `correct()`
- Unit test against known misspellings: londno, einsten, blakc, teh, recieve
- Benchmark: build time, query time

### Phase 2 — Server integration
- Load at startup, share autosuggest data (no second file load)
- Add `correction` field to `/search` response
- Log corrections to `logs/corrections.jsonl` (for future analysis)

### Phase 3 — Frontend
- Render "Did you mean: X" when `correction` is present
- Clicking it runs a new search (updates URL, results, query box)
- Style: match Google's did-you-mean presentation

---

## Files Changed

| File | Change |
|------|--------|
| `spellcheck.py` | New — SymSpell index + correction logic |
| `server.py` | Load SpellChecker at startup; add `correction` to `/search` response |
| `index.html` | Render "Did you mean" row when correction present |
| `README.md` | Document spell correction |

---

## Success Criteria

1. `londno` → suggests "london" 
2. `einsten relativity` → suggests "einstein relativity"
3. `eigenvalue` (3 results, correct spelling) → no correction offered
4. Build time < 2 seconds; correction latency < 2ms per query
5. Zero false positives on a manually-reviewed set of 20 rare-but-correct terms
