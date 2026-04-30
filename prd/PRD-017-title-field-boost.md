# PRD-017: Title-Field BM25 Boost

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-29

---

## Problem

Articles whose titles match the query strongly should rank higher than articles that just happen to mention the query terms in passing. Today they don't, and we routinely see results where Wikipedia's canonical article on a topic loses to a tangentially-related article that happened to mention the topic more times.

Concrete example — query `ozzy osbourne`:
- The article literally titled *Ozzy Osbourne* should be rank 1.
- Without title weighting, articles like *Black Sabbath* or *Randy Rhoads* often beat it because they mention "Ozzy Osbourne" many more times in their (much longer) bodies.

`wiki2trec.py` currently writes the title as the first sentence of `<TEXT>`:
```
<TEXT>
Ozzy Osbourne. Ozzy Osbourne is an English vocalist and songwriter...
</TEXT>
```

This gives titles a single occurrence of weight against BM25 — equivalent to mentioning the term once anywhere in the article. It's not enough.

The previous PRD-015 considered solving this with first-class TITLE/URL fields stored in the docmap, but that proved invasive. PRD-016's inline summariser is unrelated to ranking — it only affects snippet display.

---

## Goal

Real **field-weighted retrieval** for the title. Title-flagged term occurrences contribute more to BM25 score than body occurrences, with a tunable multiplier. The right answer architecturally — same mathematical effect as keyword-stuffing the title 3× into the body, but accomplished cleanly through a flag in the postings, configurable at query time, and not contaminating the document text used by the summariser.

When this is done, querying `ozzy osbourne` puts the *Ozzy Osbourne* article at rank 1, and the multiplier (`ZET_TITLE_BOOST`, default 3.0) can be tuned without rebuilding the index.

---

## Why this is non-trivial

Zettair already has half of what we need. From the source:

- `psettings_default.c` line 339: `<title>` is registered as a tag with `PSETTINGS_ATTR_TITLE` (`1 << 8`).
- `makeindex.c` line 344: when a title tag is open, terms inside it are still passed through to the regular indexer alongside body terms — but the title flag isn't carried into the postings.
- `summarise.c` line 458: the summariser reads the title attribute when generating snippets.
- The BM25 scorer (`okapi.c`) reads `f_dt` (term frequency in document) from the postings vector and applies `r_dt = (k1+1) * f_dt / (k1 * ((1-b) + b * D / avg_D) + f_dt)`. It has no concept of "where in the document the term appeared".

So the two missing pieces are:

1. **Postings format change**: each term occurrence in the postings needs to carry a 1-bit flag indicating "this was inside a title tag". The current format is `<docno_gap, f_dt, [offset1, offset2, ..., offset_f_dt]>` per term. We need a way to mark per-occurrence (or per-document for that term) whether the term appeared in a title.
2. **Scorer change**: when computing `f_dt`, count title-flagged occurrences as `boost * 1` instead of `1`. Or equivalently, count them separately and combine.

The postings format change is the hard part. Zettair's posting list is byte-coded vbyte-compressed, with offsets *into the document*. There's no obvious unused bit. Three options for fitting the title flag in.

---

## Design

### Approach: per-occurrence title flag in the offset stream

Postings format today (per term, per posting list):
```
<docno_gap, f_dt, [offset_gap_1, offset_gap_2, ..., offset_gap_f_dt]>
```

Where each `offset_gap_i` is the byte offset *into the document text* of the i-th occurrence of the term, encoded as a gap from the previous offset. Always positive.

Proposal: shift each `offset_gap` left by 1 bit and use the low bit as the title flag.

```
<docno_gap, f_dt, [(offset_gap_1 << 1) | title_1, (offset_gap_2 << 1) | title_2, ...]>
```

Decoders that don't know about the flag get garbage offsets — but **the offsets are only used by the summariser**, never by BM25. So the only consumer that needs to be flag-aware is the summariser (which already reads them and would ignore the low bit safely if we right-shift after decode) and the new title-boost path.

The cost: each offset takes one extra bit. In vbyte encoding, that's typically zero extra bytes for small offsets (still fits in 7 data bits per byte) and occasionally one extra byte for offsets near a vbyte boundary. Index size grows by maybe 1–3%.

### Files changed in `zettair`

**`src/postings.c`**: in `postings_addwords()`, when the `PSETTINGS_ATTR_TITLE` flag is in scope (passed down from the makeindex state machine), encode the per-occurrence offset as `(offset << 1) | 1`. Otherwise `offset << 1`.

To get the flag down into postings, `postings_addwords()` needs an extra parameter (or we widen the call interface) — it's currently called from `makeindex.c` with `(post, termbuf, len)`. Add an `attr` parameter.

**`src/include/postings.h`**: signature change for `postings_addwords()`.

**`src/makeindex.c`**: pass `mi->state->attr` (the current tag attributes from psettings) into `postings_addwords`. The state machine already tracks this — line 344 already references `PSETTINGS_ATTR_TITLE`.

**`src/okapi.c`**: new accumulator path for title-aware scoring. Read `f_dt` and the per-offset flags. Compute weighted frequency:
```c
weighted_f_dt = body_count + title_boost * title_count
```
Use `weighted_f_dt` in the BM25 formula. Add an environment variable `ZET_TITLE_BOOST` (loaded the same way `ZET_CLICK_PRIOR` and `ZET_CLICK_ALPHA` are, in `okapi_load_prior` or a new `okapi_load_title_boost`).

**`src/summarise.c`**: when reading offsets out of the postings, right-shift by 1 to get the real document position. Currently it reads them as-is — that line needs to change.

**`src/include/_postings.h`**: any internal struct that names the offset field probably needs commenting.

### Files changed in `zettair-search`

**`server.py`**: pass `ZET_TITLE_BOOST=3.0` (or whatever default) in the env when starting zet workers. Same pattern as `ZET_CLICK_ALPHA`.

**`deploy/zettair-search.service`**: add `Environment=ZET_TITLE_BOOST=3.0`.

**`wiki2trec.py`**: emit titles inside an explicit `<TITLE>...</TITLE>` tag rather than as the first sentence of `<TEXT>`. The current concatenation goes away.

```xml
<DOC>
<DOCNO>Ozzy_Osbourne</DOCNO>
<TITLE>Ozzy Osbourne</TITLE>
<TEXT>
Ozzy Osbourne is an English vocalist...
</TEXT>
</DOC>
```

### Index format compatibility

The new postings format is **incompatible** with old indexes. Old zet binaries reading new postings will see garbage offsets (and slightly wrong f_dts if our flag bit happens to leak into the count somehow). New zet binaries reading old postings will think every offset is doubled and every term is in a title.

We need a version flag in `index.param.0` to detect the mismatch. Zettair currently has no such flag — same problem PRD-015 hit. The pragmatic answer: bump `index_version` in `param.c` and refuse to load mismatched indexes with a clear error.

### Reindex required

Yes. This is a postings format change. The 1.5M corpus must be rebuilt from the existing TREC, which takes ~6 minutes (vs the multi-hour pipeline). `wiki2trec.py` itself runs again only if we want to re-emit the TREC with explicit `<TITLE>` tags — otherwise we can keep using the existing TREC and rely on the title appearing as the first sentence of `<TEXT>` (which gets parsed inside-`<TEXT>`-only with no title flag). To get the boost, we need the explicit tag.

So: re-run `wiki2trec.py` (~6 hours streaming the bz2) and `zet -i` (~10 min). All other pipeline outputs (top_titles, click_prior, autosuggest, snippets, images, urls, docstore) carry over unchanged.

---

## Boost value

`ZET_TITLE_BOOST=3.0` as a starting default. Equivalent to repeating the title three times in the body. Common values in field-weighted IR systems range from 2× to 10×; 3× is a conservative starting point that's easy to tune with an A/B if needed.

Settable per-query via env var so we can experiment without restarting:
- `ZET_TITLE_BOOST=0` — no boost (parity with current behaviour)
- `ZET_TITLE_BOOST=3.0` — moderate boost (recommended default)
- `ZET_TITLE_BOOST=10.0` — strong boost (titles dominate ranking)

---

## Risk

**This is the most invasive C work in the project.** Touching the postings format has ripple effects:

1. **Format incompatibility**: every existing index built before this change becomes unreadable. We're rebuilding anyway, but if the new format has bugs we have to choose between debugging it on the production index or maintaining two binaries.
2. **Posting size**: 1–3% larger index. Manageable. Currently 4.6 GB on the volume, would grow to ~4.7 GB.
3. **Decode compatibility**: every place in Zettair that reads offsets needs the right-shift. I've identified `summarise.c` as the only such caller, but I might have missed one. The audit needs to be careful.
4. **Test coverage**: Zettair's existing tests catch some of this (`docmap_1` etc.) but not the postings format directly. We'll need to write a new test that builds a small index and verifies title-flagged terms get the boost.

**Estimated effort:** 1–2 days of careful C work, including tests. Not a half-day fix.

---

## Files Changed (summary)

| File | Repo | Change |
|---|---|---|
| `src/include/postings.h` | zettair | `postings_addwords` signature: add `attr` param |
| `src/postings.c` | zettair | Encode per-occurrence offsets as `(offset<<1) \| title_flag` |
| `src/makeindex.c` | zettair | Pass current tag attributes through to `postings_addwords` |
| `src/okapi.c` | zettair | Read title-flagged offsets, compute weighted f_dt, apply `ZET_TITLE_BOOST` |
| `src/summarise.c` | zettair | Right-shift decoded offsets by 1 to recover real position |
| `src/index.c` (or `param.c`) | zettair | Bump index version, refuse mismatched format |
| `wikipedia/wiki2trec.py` | zettair | Emit explicit `<TITLE>` tags |
| `server.py` | zettair-search | Pass `ZET_TITLE_BOOST` to zet workers |
| `deploy/zettair-search.service` | zettair-search | Add `ZET_TITLE_BOOST=3.0` env var |

---

## Implementation Order

1. **Local test setup**: build a small TREC with 5–10 articles, some with the query word in the title, some only in the body. Index, query, verify the current behaviour (no title boost).
2. **Postings format change**: modify `postings_addwords` and the encoder. Verify the index still builds and the existing test suite passes.
3. **Summariser fix**: right-shift offsets in `summarise.c`. Verify summaries still come out correctly.
4. **Scorer change**: implement title-boost in `okapi.c`. Verify with a small test that title-matching docs rank higher.
5. **Version bump and compatibility check**: refuse to load old indexes.
6. **TREC tag emission**: update `wiki2trec.py` to emit `<TITLE>` separately.
7. **Local end-to-end test**: rebuild a 100k-doc synthetic TREC, verify boost works at scale.
8. **Server changes**: env var, service file.
9. **Push, run setup.sh on the server (which will trigger a TREC rebuild and reindex), test live.**

If anything goes wrong, the `ZET_TITLE_BOOST=0` fallback gives behaviour identical to today (modulo the format change). So worst case we land the format change without the boost active and tune later.

---

## Success Criteria

1. Query `ozzy osbourne` puts *Ozzy Osbourne* at rank 1.
2. Query `the beatles` puts *The Beatles* at rank 1, not *History of the Beatles* or *Beatles discography*.
3. Query `einstein` keeps *Albert Einstein* at rank 1 (no regression — currently works).
4. `ZET_TITLE_BOOST=0` produces ranking equivalent to current behaviour (within score-rounding tolerance).
5. Index size grows by less than 5%.
6. Mean query latency increase under 5%.

---

## What This Doesn't Do

- Doesn't touch fields other than title. URL is stored separately (PRD-015). Other potential fields (categories, infoboxes, see-also lists) stay unaddressed.
- Doesn't add general-purpose field weighting infrastructure. Only the title gets a boost. If we later want category boost or infobox boost, each is another flag bit and a similar patch.
- Doesn't replace BM25 with BM25F (the proper multi-field BM25 variant). What we're doing is a flat additive boost on title term frequency, which is mathematically a special case of BM25F with one field.
