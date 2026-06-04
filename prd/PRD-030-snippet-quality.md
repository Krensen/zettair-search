# PRD-030: Snippet Quality — Apostrophes, Sentence Boundaries, and List Junk

**Status:** Draft
**Author:** metabot
**Date:** 2026-06-04

---

## Problem

Result snippets on `/search` are technically correct (Turpin / Hawking
/ Williams query-biased fragment selection, ported from the 2003
SIGIR algorithm to Python in PRD-016) but visibly imperfect at the
**tokenisation boundary** — the strip-and-compare logic that decides
which fragments score well, and how they read once chosen.

Symptoms users see:

1. **Apostrophe-bearing queries return weaker snippets than they
   should.** A query like `children's books` parses to two terms,
   `children's` and `books`. The summariser strips non-alphanumerics
   when comparing words inside fragments, so a doc word
   `children's` becomes `childrens` after the strip, but the query
   term retains its apostrophe — so the apostrophe form never
   matches. Only the `books` half of the query is doing work; recall
   on the apostrophe-bearing half is zero.

2. **Mid-word punctuation generally gets eaten.** `non-fiction`,
   `U.S.`, `e.g.`, `it's`, `Côte d'Ivoire` all get collapsed to
   alphanumeric-only on the doc side, but the query side preserves
   them. Two-sided mismatch.

3. **Sentence splitter is naïve.** The regex `(?<=[.!?])\s+(?=[A-Z(])`
   splits after `Dr. Smith`, `J. R. R. Tolkien`, `U.S. Senate`. It
   also fails to split bullet-formatted Wikipedia content, so a
   600-character list of singles by an artist can come back as one
   "fragment" that passes the prose-verb filter on a single buried
   `was` and crowds out the actual prose sentences in the article.
   (See live evidence for `ozzy osbourne discography` — the snippet
   leads with a colon-separated bullet list rather than the article's
   lede.)

4. **Length floor of 30 chars discards good short sentences.** A 27-
   character sentence like `Iran agreed to inspections.` is rejected
   as too short, even when it is exactly the focused answer.

5. **Stray quotes survive verbatim.** Snippets sometimes start with
   `"` or end with `"` because the surrounding sentence had quoted
   material. Doesn't break correctness; reads sloppy.

6. **Query-side strict matching loses to ASCII-stripped document
   data.** Wikipedia titles drop apostrophes (`Children's Book` →
   `Children_s_Book` in docno form), and other tooling sometimes
   collapses accents. A query that preserves the apostrophe or the
   accent can fail to match an entirely valid candidate.

The algorithm itself (query-biased fragment selection, top-N by
density, prose verb-presence filter) is the right shape and not the
problem. The fix is at the *tokenisation* layer and at the
**sentence-boundary** layer.

---

## Goal

Make snippets read like newspaper ledes:

- **Recall**: apostrophe / hyphen / accented variants of a query term
  should match their doc-side cousins as if they were the same word.
- **Selection**: sentence splitter should not merge bullet lists into
  prose, or split mid-name.
- **Readability**: chosen fragments should not start/end with stray
  punctuation, should not be runaway 600-char strings, and should not
  drop the rare-but-perfect short answer.

Concretely: scoped to `summarise.py`. No changes to the FastAPI
boundary, no changes to the zet workers, no changes to the docstore.
A drop-in replacement that the existing `summarise.summarise_doc(text,
query_terms)` callers in `server.py:enrich_results()` consume
unchanged.

---

## Non-goals

- **Unicode normalisation (NFKD + strip combining marks)** — covered
  in finding #2 of the audit but explicitly deferred. The ASCII-only
  fast path handles ~95% of English Wikipedia traffic; the Unicode
  upgrade is a bigger change with measurable throughput risk in the
  inner loop. Revisit if recall on accented entities (Beyoncé, Iniesta,
  São Paulo) becomes a measurable concern.
- **Rewriting the algorithm.** We are not introducing BM25-weighted
  fragment scoring, or a learned snippet selector, or per-document
  pre-computed snippets. The Turpin/Hawking/Williams shape stays;
  only the tokenisation and boundary detection change.
- **Schema changes** to the docstore, snippets store, or search
  response. `r["snippet"]` stays a single string.
- **C-summariser revival** (PRD-016 superseded that path; do not
  re-enable `--summary=plain` on the zet workers).
- **Stopword/stemming changes.** The stopword set is fine; revisiting
  it is a separate question.
- **Per-token boosts** like phrase matching, proximity scoring, or
  field weighting. Stays out of scope.
- **Snippet caching.** Per-query, per-doc snippets are unique by the
  query terms, so caching would have a very low hit rate.

---

## What ships

Five fixes, all in `summarise.py`. Ordered by visible impact.

### 1. Apostrophe-aware query and doc tokenisation (#1 from audit)

The clean-and-compare cycle is the root cause of the apostrophe miss.
Fix:

- In `parse_query()`, when a query term contains an apostrophe, emit
  **both forms** into the term set: the apostrophe form (`children's`)
  AND the stripped form (`childrens`). Same for hyphen.
- In `_score_and_check()`, when a word contains an apostrophe or
  hyphen, generate the same stripped form via `translate` and check
  *both* forms against the query set. Hits count once per matched
  position, not per form (so `children's` matching both forms in the
  set still scores 1 hit, not 2).

Net: a query for `children's books` matches both `children's books`
and `childrens books` in the article body. A search for `Beyoncé` is
unaffected (no apostrophe), but a search for `O'Brien` matches both
`O'Brien` and `OBrien`.

Estimated cost: ~10 lines of code, negligible runtime overhead
(`'` in `t` short-circuits the common path).

### 2. Better sentence splitting (#3 from audit)

Replace the single regex with a small pipeline:

- **Hard splits first**: split on `\n\n` (paragraph breaks), on
  bullet markers (`* `, `– `, `• `, `· ` at line start), and on
  semicolons inside very long lines (>200 chars). This is what
  rescues the Ozzy discography case.
- **Soft splits second**: the existing `(?<=[.!?])\s+(?=[A-Z(])`
  regex, but with a **non-split lookahead** for common
  abbreviations. The cheapest path is a small set of
  abbreviation prefixes — `Dr.`, `Mr.`, `Mrs.`, `Ms.`, `Jr.`,
  `Sr.`, `St.`, `vs.`, `U.S.`, `U.K.`, `etc.`, `e.g.`, `i.e.`,
  `cf.` — and a post-split merge step that reconnects fragments
  whose left side ends in one of these.
- **Citation marker stripping**: before splitting, drop inline
  `[NN]` patterns from the text. Wikipedia citations leak through
  the docstore and produce snippets like `as noted.[12] Following`.

Estimated cost: ~30 lines, including the abbreviation set and the
merge step. Inner-loop cost is still O(text-length).

### 3. Strip stray edge quotes from chosen fragments (#4 from audit)

After top-N selection, before joining with ` … `:

```python
fragment = fragment.strip().strip('"“”‘’')
```

(Both ASCII and typographic quotes.) Easy.

Estimated cost: 2 lines.

### 4. Lower minimum length, cap maximum length (#5 from audit)

Two tweaks:

- **Minimum drops 30 → 20 chars.** A 25-character sentence like
  `Iran agreed to inspections.` is the best possible answer for a
  query like "iran inspections". Today it's rejected.
- **Maximum cap of 240 chars on a single fragment.** Anything longer
  is almost certainly a bullet-list paragraph, an unsplit citation
  block, or an infobox. Score the first 240 chars only; or split at
  the nearest `,` / `;` if doing so produces a sub-fragment under
  240. Reduces the "long junk fragment dominates by accident"
  failure mode.

Estimated cost: ~15 lines including the soft-split-on-comma logic.

### 5. Query-side variant generation (#6 from audit)

Complement to #1. When the user types a multi-form word, we already
emit two forms (#1). Extend to handle other variant patterns:

- **Hyphen variants**: `non-fiction` → `{non-fiction, nonfiction,
  non fiction}` (we already match the docstore via #1; the third
  variant covers query-side hyphen vs doc-side space).
- **Possessive `'s` stripping**: `Tolkien's` → `{tolkien's, tolkien}`.
  This is what makes a search for `Tolkien's books` rank pages
  about Tolkien even when they say `Tolkien wrote` rather than
  `Tolkien's books`.

Estimated cost: ~5 lines in `parse_query`.

---

## Edge cases

**1. Apostrophe types.** Wikipedia text and modern publishing use the
typographic apostrophe `'` (U+2019) interchangeably with ASCII `'`.
Treat both as equivalent in tokenisation; normalise to ASCII before
hashing into the term set so both forms collide.

**2. Hyphen vs en-dash vs em-dash.** ASCII `-`, en-dash `–`, em-dash
`—` should all act as either word-internal hyphens (`non-fiction`) or
fragment separators (`Smith — the Australian PM — said today`). The
sentence splitter should treat em-dash with surrounding spaces as a
soft split; embedded hyphens stay attached.

**3. Possessive on plural-s.** `Smiths'` (plural possessive) should
behave like `Smiths`. The strip-apostrophe variant covers this for
free.

**4. Numeric tokens with periods.** `3.14`, `9.8`, `1.5%` — the
sentence splitter must not split here. The existing
`(?=[A-Z(])` lookahead handles this in most cases; verify on cases
like `was 3.5. Tons` (split correctly) vs `was 3.5 tons` (no
split).

**5. Multi-line bullet text in the docstore.** Wikipedia infobox
content sometimes arrives as `*Item 1\n*Item 2\n*Item 3`. The
`\n\n` paragraph-split rule doesn't cover this (single newlines).
Adding `\n` followed by `*` or `•` to the hard-split list catches
it.

**6. Short fragments containing only the query term.** A 20-char
fragment that is just the query echoed back (e.g. doc title text)
should NOT score 1.0. The verb-presence filter handles this
correctly today and stays.

**7. Case sensitivity.** Everything stays lowercase-compared. Doc
fragments are stored case-preserved (we render them); only the
matching is case-insensitive. No change.

**8. Multiple queries per fragment.** A fragment with 5 hits should
still score higher than one with 2; we keep `hits / len(words)` as
the score. The variant changes raise the hit count in some cases
without changing the structure.

---

## Quality measurement

Hard to A/B test without instrumenting click-through, which we don't
have at the per-snippet grain. So:

- **Hand-curate a set of ~30 query-result pairs** with currently
  ugly snippets, ranked by ugliness. Examples drawn from this PRD:
  - `children's books` — apostrophe miss
  - `ozzy osbourne discography` — bullet list as one fragment
  - `j r r tolkien` — abbreviation splits
  - `civic technology` — citation noise
  - `women in technology` — common-word baseline
- Capture the *current* snippet for each before deploying.
- After deploying, capture the *new* snippet.
- Mark each pair as **improved / same / regressed**.
- Ship when improved >> regressed (target: ≥20 improved, ≤2
  regressed, rest same).

Per-step rollout: ship steps in isolation if possible (separate
PRs) so each can be evaluated independently. Step 1 (apostrophes) is
the most measurable individually.

Latency: the existing summariser averages ~3-5 ms per result at the
n=10 default (per `phases.summary_ms` in `/search` responses). The
proposed changes add at most one more `translate` call per word and
one more set lookup. Expect <10% regression in `summary_ms`, well
within the noise floor on a per-query basis.

---

## Risks

- **Variant explosion in `parse_query`.** A query like `it's not
  rocket science's children's books` generates a lot of variants.
  Cap each query at, say, 16 unique terms (the set dedupes most
  duplicates automatically).
- **Sentence splitter regressions.** The added abbreviation set is
  English-only; non-English Wikipedia article titles in the corpus
  (e.g. translated names) might behave differently. Spot-check on
  pages with heavy French / German content.
- **Bullet-list paragraphs that are actually informative.** A list
  of "key facts" in an infobox might *be* the right snippet for a
  factual query. The max-fragment cap could trim a useful fragment.
  Mitigation: max cap is only applied to *score*, not to *display*
  — the displayed fragment can be the full original, just with the
  first 240 chars used for ranking. Decide during implementation.
- **Typographic-apostrophe normalisation breaks search-result
  highlighting in the frontend.** `highlightQuery()` in `index.html`
  splits the query on whitespace and word-matches; if it's hashing
  against `it's` and the doc displays `it's`, the highlight regex
  needs an OR clause. Easy to handle at frontend later if it
  matters; flag as follow-up.

---

## Build estimate

Working solo, no parallelism, in `summarise.py` only:

| Step | Time |
|---|---|
| #1 (apostrophe-aware tokenisation) | 1 h |
| #3 (sentence splitter + abbreviations + bullet splits) | 2-3 h |
| #4 (edge quote strip) | 15 min |
| #5 (length floor/cap, soft-split on comma) | 1 h |
| #6 (query-side variant generation) | 30 min |
| Hand-curated snippet eval (30 pairs) | 1 h |
| **Total** | **~5-6 h** |

Defer #2 (full Unicode normalisation) — not part of this PRD.

---

## Plan

1. Branch off main.
2. Apply #1 + #4 + #6 together (smallest, all about tokenisation).
   PR + merge. Run the eval — should see the apostrophe wins.
3. Apply #3 (sentence splitter) on a follow-up branch. PR + merge.
   Re-run eval. The bullet-list and abbreviation wins land here.
4. Apply #5 (length floor / cap). Smallest cleanup pass.
5. Final eval over the same hand-curated set. Update CHANGELOG /
   PRD-016 with the changes.

If any single step regresses ≥3 of the curated pairs, revert that
step and iterate on it.

---

## Open questions

1. **Score-vs-display split for the max-length cap.** Easier to ship
   as "score the first 240 chars, display the full original
   fragment." Means a long bullet list could still appear in
   the result, but only when its prefix happens to score well. Lean
   toward this.
2. **Whether to add `'s` stemming to the query side proactively, or
   only when no hits.** Cheaper to do it always (the set dedupes);
   slightly increases recall on the queries that needed it. Go
   with always.
3. **Abbreviation list maintenance.** A frozen set in code is the
   simplest. Worth pulling from a Wikipedia category at some point
   but not for v1.

---

## Addendum (2026-06-04, post-step-C deployment)

Three follow-ups identified after live verification of steps A-C:

### #7 Title display drops apostrophes and other URL-safe punctuation

Observed: `https://zettair.io/search?q=Putin%27s%20Palace` returns
result rows where the URL is correctly `…/Putin's_Palace` but the
displayed title reads "Putin s Palace" — the apostrophe is gone in
the docno form (Wikipedia's safe_id collapses non-alphanumeric chars
to `_`), and the frontend reconstructs the title from the docno
rather than from the dbkey-bearing URL.

`server.py:enrich_results()` already has the canonical URL available
per result via `_urls_store.get(docno)` — for the ~23% of articles
where the dbkey differs from the safe_id, the URL preserves the
apostrophe / period / parenthesis. The frontend's `formatTitle()`
just doesn't use it.

Two equivalent fixes:

- **Frontend-side**: derive the title from `r.url` when the URL
  contains characters that the docno cannot. Pros: zero server
  change. Cons: every client (web, iOS PRD-028, future API consumers)
  reimplements.
- **Server-side**: add an explicit `title` field to the
  `enrich_results` output, derived from the URL when one is in
  `_urls_store`, else from the docno. Single source of truth.

Lean **server-side**. Cleanest; iOS gets the fix for free; web
frontend's `formatTitle` becomes `r.title || formatTitle(r.docno)`
as a graceful fallback.

### #8 Citation residue and quote-imbalance leak into snippets

Observed: queries for "Putin khuylo" and "Putin's Kleptocracy" return
snippets like `Putin khuylo!. , May 2014 "Putin …` and
`...accusation that "Putin and his close colleagues have enriched
themselves is now effectively proven" and "a courageous and…` — the
content is correct prose mixed with citation-shaped residue (lone
comma + date + dangling quote, or runs of multiply-quoted clauses
that confuse the eye).

The wiki markup itself (`{{}}`, `[[]]`, `'''`) has already been
stripped upstream in the docstore. What survives is:

- Floating dates and short stray phrases left over from removed
  citation templates: `, May 2014`, `(2018)`, `(in Russian)`.
- Odd-numbered quote counts: fragment opens `"` but doesn't close.
- Sentences that start with `,` `;` `(` or a digit-year.

Add a residue filter inside `_score_and_check`:

- If the fragment, after edge-stripping, starts with one of
  `,;:)(` or a digit followed by a year, reject (return 0).
- If the count of `"` + `"` + `"` is odd, reject. (Already a
  proxy for "something got cut in half".)
- If the fragment contains a 3+ char run of pure punctuation
  separators (`. , `, `". `, `).` patterns), and the run is in
  the first 25% of the fragment, reject.

Cost: ~10 lines, no algorithmic change.

### #9 Bullet-marker regex misses `: *` and `, *` (the actual Wikipedia
pattern)

Observed: `Ozzy_Osbourne_discography` snippet still leads with
`UK Singles Chart peak positions for Ozzy Osbourne singles as
featured artist: *"Close My Eyes Forever": *"Hey Stoopid": ...` —
the bullet items use `: *` as a separator, not `\s*`.

Current `_RE_HARD_SPLIT` requires whitespace **before** the bullet:
`(?<=\s)[*•·–—]\s+` matches `text * "Title"` but not `text: *"Title"`.
Real Wikipedia export uses `:` and `,` as the separator more often
than the space, because the bullet markers come from list-rendering
fallback (e.g. table rows joined inline).

Widen the regex to also accept `:` and `,` before the bullet:

```python
_RE_HARD_SPLIT = re.compile(
    r'\n{2,}'
    r'|(?<=[\s:,;])[*•·–—]\s*'  # bullet after whitespace OR punctuation
    r'|\n[*•·]\s*'
    r'|\s+[–—]\s+'
)
```

Note the `\s*` (was `\s+`) after the bullet marker — Wikipedia text
often immediately quotes after the bullet (`*"Title"`).

Cost: 1-line regex change + verify on the Ozzy case.

### Build estimate (addendum)

| Step | Time |
|---|---|
| #7 (server-side title field) | 30 min |
| #8 (residue filter) | 1 h |
| #9 (bullet regex widening) | 15 min |
| **Total** | **~2 h** |

Ship as a single Step D PR — all three are small, contained, and
non-interacting.
