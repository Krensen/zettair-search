# PRD-027: Reading Time + Difficulty Signal on Every Result

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-17

---

## Problem

A search result on Wikipedia gives you almost no signal about what you
are about to commit to. Two articles that look identical in the
results list can be wildly different:

- A 600-word stub vs. a 12,000-word featured article — the same
  one-line snippet hides a 20× difference in reading time.
- "Bayes' theorem" (technical, dense, math-heavy) vs. "Cat" (plain,
  approachable, narrative) — the snippet is no help in distinguishing
  them at a glance.

This costs users in two ways:

1. **Wasted clicks.** People who want a quick scan open a 30-minute
   read; people who want depth open a stub. Either way they bounce
   back to the results.
2. **Decision paralysis.** Students and researchers especially want
   to triage results before committing — "is this one I should read
   now, or save for later?" Without a signal, every result is
   opaque.

Wikipedia itself surfaces neither metric. We can compute both cheaply
from text we already have on disk, and surface them as two unobtrusive
pills on every result card. The signal is small but ambient — useful
on nearly every search, invisible enough that it never gets in the
way.

This is item #4 in PRD-023's feature ideas list, called out there as
"quietly useful on every result."

---

## Goal

Every search result, and the knowledge panel, gets two small pills
under the title:

- **Reading time** — e.g. "5 min read" — derived from word count at
  250 wpm.
- **Difficulty** — `accessible | moderate | technical` — bucketed
  from Flesch-Kincaid grade level.

Both computed once, offline, from the docstore. Stored in a sidecar.
Loaded into RAM at server startup. Looked up O(1) per result. No
LLM, no network, no third-party data. Pure local computation.

---

## Non-goals

- **Per-query difficulty.** The difficulty pill is per-article, not
  per-query. A user querying "quantum mechanics for beginners" still
  sees the same difficulty rating on the *Quantum mechanics* article
  as a user querying "QED Feynman path integral". Reasonable v1
  scope; the difficulty is a property of the article, not the search
  intent.
- **Multi-language reading speed.** 250 wpm is the English-prose
  average. We don't internationalise this in v1; corpus is
  English-only anyway.
- **Reading-level personalisation.** No user setting for "show me
  technical articles first" or "filter to accessible". Just the
  signal. Filtering / sorting is a follow-up if the signal proves
  useful.
- **CMU-dict syllable accuracy.** We use a vowel-group heuristic
  for syllables (~0.5% error vs. CMU dict). Difficulty buckets
  are coarse enough that this doesn't matter.
- **Live updates.** The sidecar is rebuilt with the corpus. Edits
  on Wikipedia between rebuilds don't change the pills until the
  next rebuild.

---

## High-level design

```
result card:
  ┌──────────────────────────────────────────────────┐
  │ [favicon] Wikipedia · en.wikipedia.org › Bayes'_theorem
  │ Bayes' theorem                          [Cite]
  │ ┌──────────────┐ ┌────────────┐
  │ │ 7 min read   │ │ technical  │
  │ └──────────────┘ └────────────┘
  │ In probability theory and statistics, Bayes'…   [thumbnail]
  └──────────────────────────────────────────────────┘
```

Two pills, neutral-grey, same row, just below the title. Tooltip on
each ("Approx. reading time at 250 wpm" / "Flesch-Kincaid grade level
{n}"). Hidden entirely for articles where the signal would be
unreliable (very short stubs, list / disambiguation pages).

---

## Pieces

### 1. Offline computation

A new standalone script: `tools/build_reading_sidecar.py`.

Reads the docstore (1.5M plain-text articles) once. For each article:

- **Word count.** Tokenise on whitespace after stripping punctuation.
- **Sentence count.** Split on `[.!?]` followed by whitespace.
- **Syllable count.** Per-word vowel-group regex
  `re.findall(r'[aeiouy]+', word)`. Floor at 1 per word.
- **Flesch-Kincaid grade.**
  `0.39 * (words/sentences) + 11.8 * (syllables/words) - 15.59`.
- **Reading time minutes.** `max(1, round(words / 250))`.
- **Difficulty bucket.**
  - `accessible` if FK ≤ 8
  - `moderate` if 8 < FK ≤ 13
  - `technical` if FK > 13
- **Suppression.** If `words < 150` or `sentences < 5`, emit
  difficulty `null` (reading-time still emitted, but never below
  the 1-minute floor).

Output: a single binary sidecar at
`/mnt/wikipedia-source/enwiki_top1m.reading.bin`. Format:

```
4-byte magic "RDT1"
4-byte uint32: entry count N
N entries of:
  variable-length docno (length-prefixed, uint16 + utf-8 bytes)
  uint16: reading_time_min
  uint8:  difficulty code (0=null, 1=accessible, 2=moderate, 3=technical)
```

Estimated size: ~9 MB packed. Acceptable in RAM.

A `.json` form is fine if we'd rather eyeball it during tuning;
defer to whoever builds it.

Runtime estimate: ~5-10 minutes for the full corpus. Single-threaded
Python; can be parallelised if it ever becomes a bottleneck.

### 2. Server load + lookup

On startup, `server.py` reads the sidecar into two dicts:

```python
_reading_time:  dict[str, int]            # docno -> minutes
_difficulty:    dict[str, str | None]     # docno -> "accessible"|"moderate"|"technical"|None
```

In `enrich_results()`, for each result, merge:

```python
result["reading_time_min"] = _reading_time.get(docno)
result["difficulty"]       = _difficulty.get(docno)
```

Same pattern for the knowledge panel — populate the existing
response object with the two new fields.

Missing values are tolerated end-to-end: the frontend hides the pill
when the value is `null` or absent.

### 3. setup.sh staleness gating

Add a `build-reading-sidecar` step after the docstore step:

- **Skip if** sidecar exists AND is newer than the docstore.
- **Rebuild if** sidecar is missing OR docstore is newer.

One mtime comparison, cheap. The downstream (server load) genuinely
consumes the output, so this satisfies the "staleness triggers must
have a downstream that uses them" rule.

Reuses the existing `is_stale` helper. ~5-line addition.

### 4. Frontend pills

Two new pill elements in the result-card template
(`index.html:1282-1303`):

```html
<div class="result-meta">
  <span class="meta-pill reading-time">7 min read</span>
  <span class="meta-pill difficulty difficulty-technical">technical</span>
</div>
```

Pill colours:

- Reading time: neutral grey, same for all.
- Difficulty:
  - `accessible` → soft green
  - `moderate` → soft amber
  - `technical` → soft red

Subtle, low-contrast — the result title still dominates the card.
Tooltips give the precise FK grade for power users.

Same pattern on the knowledge panel header, where they fit naturally
under the article title.

Render skipped when the field is `null` or absent.

---

## Edge cases

**Very short stubs.** Below 150 words or 5 sentences, FK is
unreliable. Reading-time still shows (always ≥1 min); difficulty pill
is suppressed.

**List / disambiguation pages.** These don't have prose. Their FK
scores are misleading. We can detect them by:

- Title patterns: `^List_of_`, `_(disambiguation)$`, `^Outline_of_`.
- Or: sentence count < 5 (catches most, including the title-pattern
  cases that have no prose).

Recommend sentence-count threshold as the primary filter — title
patterns are a fallback if the threshold proves too lax.

**Heavy-math articles.** Some technical articles have very few
sentences in the rendered text because most of the content is
inline equations stripped to `formula` tokens. The FK score on the
prose-only remainder is fine — it reflects how readable the
*surrounding text* is, which is what users care about.

**Featured articles.** These are long (30+ min read on the high end).
The pill becomes load-bearing for the "do I commit to this?" question.
No special handling needed; the pill just does its job.

**Rounding.** "1 min read" is fine for floor. We don't show "0 min".
For long articles, round to nearest minute up to 60; cap display at
"60+ min read" if anything overshoots. Realistically nothing in our
corpus crosses an hour, but the cap is defensive.

---

## Difficulty threshold tuning

The 8 / 13 grade-level bucket boundaries are a starting point, not a
final answer. Plan:

1. Build the sidecar with provisional thresholds.
2. Sample ~50 articles across the corpus — random plus a curated
   "obvious accessible / obvious technical" hand-list.
3. Eyeball the bucket assignments. Adjust thresholds (still just two
   numbers) until the labels match human judgement on the
   hand-list and look sane on the random sample.
4. Recompute. ~10 min total per iteration since the sidecar rebuild
   is independent of corpus rebuild.

This is the main reason we want a standalone sidecar rather than
folding into `build_docstore.py` for v1: tuning iterations stay
cheap.

---

## Folding into the docstore (v1.5)

Once thresholds are settled and we've validated the signal is
useful, fold the computation into `build_docstore.py` and remove the
standalone sidecar:

- Each docstore entry gets a small JSON header prepended:
  `{"rt": 7, "diff": "technical"}\n---\n<article text>`.
- The few docstore readers (~3 callers) learn to split the
  separator and expose the header fields.
- Delete `tools/build_reading_sidecar.py`, the sidecar file, the
  setup.sh staleness step, the in-RAM dicts at server startup.

Net: one fewer file on disk, one fewer staleness rule, same user
experience. ~30 min cleanup PR. No data migration needed — both
files are derived.

We do *not* commit to a date for v1.5. The sidecar is a perfectly
fine resting place if folding never becomes a priority.

---

## Why this works

- **Cheap signal, real value.** Two pills under every result; takes
  no real screen space; answers a question users have on most
  searches without realising it.
- **No model risk.** Pure arithmetic on text we already have. No
  prompt tuning, no API budget, no flakiness, no rate limits.
- **Differentiator.** Wikipedia itself gives you nothing here. Google
  estimates "X min read" only for explicit longform content
  (Medium, NYT). On Wikipedia results, we're the only ones surfacing
  it.
- **Composable with what we ship next.** A future "Show me only
  accessible articles" toggle on the results page becomes trivial
  once the field is in every response. A future "estimated time to
  read all results on this query" sum is one line of JS.

---

## Open questions

- **Pill text precision.** "7 min read" vs. "~7 min" vs. "7 min".
  Lean toward the plain "7 min read" — matches Medium / standard
  reading-time UX.
- **Difficulty wording.** `accessible | moderate | technical` is
  one option. `easy | medium | hard` is another but feels
  pejorative for Wikipedia. `general | intermediate | advanced`
  is a third. Lean toward `accessible / moderate / technical` —
  matches the PRD-023 brief and avoids value-laden wording.
- **Tooltip content.** Just the FK grade ("Grade level 14") or
  fuller explanation? Recommend the grade level — short, factual,
  power-users can look up FK if they want context.

Resolve in build by picking sensible defaults and adjusting if user
feedback or our own usage suggests otherwise.

---

## Build estimate

| Step | Time |
|---|---|
| `tools/build_reading_sidecar.py` (read docstore, compute, emit) | 2-3 h |
| Server load + `enrich_results()` lookup | 1 h |
| `setup.sh` staleness gating | 30 min |
| Frontend pills + CSS | 1-2 h |
| Difficulty threshold tuning (~2 iterations) | 1-2 h |
| **Total** | **~1 day** |

Matches PRD-023's 1-2 day estimate. Lean toward 1 day with no
surprises, 1.5 if thresholds want a third tuning pass or the
short-article suppression rule needs revisiting.

---

## Risks

- **Stub-noise.** If the suppression rule is too generous, lots of
  short articles show without a difficulty pill. Visually fine — we
  just lose the signal on those. Tune the threshold.
- **Reading-time gaming.** Articles with heavy template content
  (infoboxes, tables) have inflated word counts vs. their actual
  prose. Acceptable for v1 — the docstore is plain text already, so
  most template clutter has been stripped during `build_docstore.py`.
  If it proves an issue, we can subtract template-token counts in
  the builder.
- **User indifference.** Possible nobody notices or cares. Cheap
  enough that this is fine — we lose ~1 day if it falls flat. The
  feature also enables future filtering / sort options that would
  surface the data more prominently.
