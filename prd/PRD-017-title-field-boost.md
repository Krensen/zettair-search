# PRD-017: Field-Weighted Retrieval (Title First, Generalises to N Fields)

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-29

---

## Problem

Articles whose titles match the query strongly should rank higher than articles that just happen to mention the query terms in passing. Today they don't.

Concrete example — query `ozzy osbourne` on a small synthetic corpus:

| Doc | Score | Mentions in body | Mentions in title |
|---|---|---|---|
| Ozzy_Osbourne | 4.47 | 0 | yes |
| Black_Sabbath | 8.05 | 5 | no |
| Randy_Rhoads | 8.18 | 5 | no |

The actual *Ozzy Osbourne* article comes third because BM25 has no concept of "where" the term appeared. The same problem happens on the 1.5M production corpus: searching for the canonical name of a band/person/concept routinely loses to a tangentially-related article that happened to mention them many times.

Title is the most obvious case but not the only one. Image captions are good signal too — Wikipedia articles often have very short descriptive captions that are highly informative about the article. Categories, see-also lists, and infobox fields fall in the same bucket: short, semantically significant text that should outweigh body mentions of the same terms.

So the design should not be "boost titles" but "boost N kinds of fields, of which title is the first".

---

## Goal

A general field-weighting mechanism that:

1. Lets `wiki2trec.py` emit content inside named tags (`<TITLE>`, `<CAPTION>`, `<CATEGORY>`, ...).
2. Records per-occurrence which field each term came from in the postings.
3. Lets `okapi.c` apply a per-field multiplier when computing BM25 term frequency.
4. Is configurable at query time per field (`ZET_BOOST_TITLE`, `ZET_BOOST_CAPTION`, ...).
5. Generalises to up to ~16 distinct fields without further format changes.

For the first cut, only the **title** field is wired all the way through. Other field IDs are reserved in the format but not yet emitted by the indexer — adding them later is "register a tag in psettings, set an env var", not a re-architecture.

When this is done:
- Query `ozzy osbourne` puts *Ozzy Osbourne* at rank 1.
- Adding image captions later is one diff per repo, no format change.

---

## Why a real field mechanism rather than keyword-stuffing

Keyword-stuffing the title (writing it 3× into `<TEXT>`) gives the same BM25 mathematical effect for one field. But:

- It doesn't generalise. Stuffing title + caption + category + see-also into `<TEXT>` produces text that's increasingly junky.
- It contaminates the document text used by the summariser. The summariser would see "Ozzy Osbourne. Ozzy Osbourne. Ozzy Osbourne. He is an English vocalist..." and the prose-quality filter would have to start filtering out repeated title sentences.
- The boost is fixed at index time. Tuning it requires a reindex.

A real field mechanism is more invasive but is the right architecture for the system we're heading towards.

---

## Design

### Postings format change: per-occurrence field-id

Today, postings store offsets per term per document:

```
<docno_gap, f_dt, [offset_gap_1, offset_gap_2, ..., offset_gap_f_dt]>
```

Each `offset_gap_i` is the byte offset (gap-encoded) of the i-th occurrence of the term. Always positive.

Proposal: shift each `offset_gap` left by 4 bits and use the low 4 bits as the **field-id**.

```
<docno_gap, f_dt, [(offset_gap_1 << 4) | field_1, (offset_gap_2 << 4) | field_2, ...]>
```

Field-id assignment:
- `0` = body (default — anything not inside a recognised field tag)
- `1` = title
- `2` = image caption *(reserved, not emitted yet)*
- `3` = category *(reserved)*
- `4` = see-also *(reserved)*
- `5` = infobox *(reserved)*
- `6-15` = unallocated, available for future use

The cost: each offset takes 4 extra bits. In vbyte encoding, that's typically 0-1 extra bytes per offset. Index size grows by ~5%. For 1.5M corpus today (4.6 GB index), the new index would be ~4.8 GB. Trivial.

### Files changed in `zettair`

**`src/include/postings.h`**: `postings_addwords()` gains a `field_id` parameter.

```c
int postings_addwords(struct postings *post, char *text, unsigned int len,
                      unsigned int field_id);
```

**`src/postings.c`**: when encoding an offset, OR in the field-id at the bottom 4 bits.

**`src/include/_postings.h`**: define `MAX_FIELDS 16` and a few helpful macros.

**`src/makeindex.c`**: track the current field-id as parser state. When `PROCESS_TAG` recognises a field-flagged tag (e.g. one with `PSETTINGS_ATTR_TITLE`), flush the current `termbuf` first (so the just-buffered terms are committed with the *previous* field-id), then update the field-id. Same on tag close. Pass the current field-id into every `postings_addwords` call.

**`src/include/psettings.h`**: extend the attributes enum so each field gets its own bit. Today we have `PSETTINGS_ATTR_TITLE = 1<<8`. Add `PSETTINGS_ATTR_CAPTION = 1<<9`, etc. (Or, more cleanly, add a new field-id member to the psettings type — but that requires a config-file format change too. The bit-flag approach is more conservative.)

A helper function `psettings_field_id(attr)` returns the integer field-id corresponding to the field bits in `attr`, or 0 if no field bit is set.

**`src/psettings_default.c`**: register the tags. `<title>` already there. Add stubs for `<caption>`, `<category>`, etc. (registered but with attribute bits that don't get used until those tags are emitted by wiki2trec).

**`src/okapi.c`**: 
- Per-field boost values, loaded from env vars `ZET_BOOST_TITLE`, `ZET_BOOST_CAPTION`, etc., at startup. Stored in a `double field_boost[MAX_FIELDS]` array, defaulting to 1.0 (no boost).
- When iterating posting offsets to compute `f_dt`, decode the field-id from the low 4 bits and accumulate `weighted_f_dt += field_boost[field_id]` instead of `f_dt += 1`.
- Use `weighted_f_dt` in the BM25 formula in place of `f_dt`.

**`src/summarise.c`**: decode offsets with `>> 4` to recover the real document position.

**`src/index.c`** (or `param.c`): bump `index_version`. New zet refuses to load old indexes with a clear error message.

### Files changed in `zettair-search`

**`wikipedia/wiki2trec.py`**: emit explicit `<TITLE>...</TITLE>` tags around the article title rather than concatenating the title into the first sentence of `<TEXT>`. Future fields (when added) get their own tags too.

```xml
<DOC>
<DOCNO>Ozzy_Osbourne</DOCNO>
<TITLE>Ozzy Osbourne</TITLE>
<TEXT>
He is an English vocalist...
</TEXT>
</DOC>
```

**`server.py`**: pass `ZET_BOOST_TITLE` (initially 3.0, later other field boosts) in the env when starting zet workers.

**`deploy/zettair-search.service`**: add `Environment=ZET_BOOST_TITLE=3.0`.

### Index format compatibility

The new postings format is incompatible with old indexes. Bump the index version flag. The new zet binary refuses to load an old index with an error like:

```
ERROR: index format version 2 not supported (this binary expects version 3).
       Run 'zet -i' to rebuild.
```

A reindex of the 1.5M corpus takes ~10 minutes. The TREC needs to be re-emitted by `wiki2trec.py` to use explicit `<TITLE>` tags — that's a 4-8 hour run. We'd skip the bz2 download (already on disk), so total deploy time ~5 hours.

### Per-field tuning

`ZET_BOOST_TITLE=3.0` as a starting default. Common values in field-weighted IR systems range from 2× to 10×; 3× is a conservative starting point.

Settable via env var so we can experiment without rebuilding:
- `ZET_BOOST_TITLE=0` — disable title boost (titles count as body)
- `ZET_BOOST_TITLE=3.0` — recommended default
- `ZET_BOOST_TITLE=10.0` — strong boost, titles dominate

Same env-var pattern as the existing `ZET_CLICK_ALPHA` for click prior. When we add caption, it'd be `ZET_BOOST_CAPTION=2.0` — same pattern, no code change.

### What this does *not* tackle

- **BM25F**: the proper multi-field BM25 variant treats each field independently, with its own `k1`/`b` and length normalisation. We're doing a flatter additive boost on `f_dt`. BM25F is the next step if/when we want it.
- **Field-restricted queries** (`title:einstein`): would require query-time filtering on field-id, plus query syntax extension. Different feature, can come later.
- **Stop-list per field**: maybe titles want different stopword handling than body. Out of scope.

---

## Memory and disk impact

| Resource | Before | After (1 field active) | After (4 fields active) |
|---|---|---|---|
| Index size | 4.6 GB | ~4.8 GB | ~5.0 GB |
| Server RAM | ~700 MB | ~700 MB | ~700 MB |
| Indexing time | ~10 min | ~10 min | ~10 min |
| Per-query overhead | — | <1 ms | <1 ms |

Negligible.

---

## Risk

This is the most invasive C work in the project. Touching the postings format affects:

1. **Format incompatibility**: every existing index built before this change becomes unreadable. Rebuild is required.
2. **Decode compatibility**: every place in Zettair that reads offsets needs the `>> 4` shift. Audit identifies `summarise.c` as the only such caller; a careful grep is required.
3. **State machine in `makeindex.c`**: tracking field-id across nested tags and term buffer flushes is fiddly. Risk of off-by-one or missed flushes that mis-attribute terms to the wrong field.
4. **Test coverage**: zet's existing tests don't cover the new format. Need a new test that builds a small index with title content and verifies title-flagged terms get the boost.
5. **Backout**: if the implementation has bugs, rollback is reverting the patch and rebuilding the index against the old binary. ~5 hours.

Mitigation: **`ZET_BOOST_TITLE=0` produces ranking equivalent to current behaviour** (modulo the format change). If the boost logic is wrong the worst case is "we shipped a format change but the ranking is back to baseline", and we tune from there.

**Estimated effort**: 1-2 days.

---

## Files Changed (summary)

| File | Repo | Change |
|---|---|---|
| `src/include/postings.h` | zettair | `postings_addwords` gains `field_id` param |
| `src/postings.c` | zettair | Encode offsets as `(offset<<4) \| field_id` |
| `src/include/_postings.h` | zettair | `MAX_FIELDS` define and helper macros |
| `src/include/psettings.h` | zettair | Add `PSETTINGS_ATTR_*` bits per field; `psettings_field_id()` helper |
| `src/psettings_default.c` | zettair | Register tag stubs for future fields |
| `src/makeindex.c` | zettair | Track current field-id; flush on transitions; pass to postings |
| `src/okapi.c` | zettair | Read `ZET_BOOST_*` env vars; weight `f_dt` per-occurrence |
| `src/summarise.c` | zettair | Right-shift offsets by 4 to recover real position |
| `src/index.c` (or `param.c`) | zettair | Bump index version, refuse mismatched format |
| `wikipedia/wiki2trec.py` | zettair | Emit `<TITLE>` tags explicitly |
| `server.py` | zettair-search | Pass `ZET_BOOST_TITLE` to zet workers |
| `deploy/zettair-search.service` | zettair-search | Add `ZET_BOOST_TITLE=3.0` |

---

## Implementation Order

1. **Local test setup**: small TREC with explicit `<TITLE>` tags. Index, query, capture baseline (no title boost). *(Already done.)*
2. **psettings & makeindex**: add field-id tracking through the parser. Flush termbuf on field transitions. Verify the index still builds and existing tests pass.
3. **postings format**: encode field-id in low 4 bits of offset. Bump version.
4. **summariser fix**: right-shift offsets by 4. Verify summaries still work.
5. **scorer**: read `ZET_BOOST_TITLE` at startup, decode field-id per posting, apply boost. Verify Ozzy_Osbourne now wins on the local test.
6. **wiki2trec.py**: emit `<TITLE>` tags. Verify roundtrip.
7. **End-to-end test**: re-index, run a battery of queries.
8. **Push, deploy, rebuild on server.**

If any step uncovers a deeper format issue or unforeseen complexity, stop, document, and reassess. This PRD's success or failure is uncovered by step 2 — if tracking field-id through the parser turns out to require restructuring the parser itself, the cost-benefit shifts.

---

## Success Criteria

1. Query `ozzy osbourne` puts *Ozzy Osbourne* at rank 1 on the local test corpus.
2. Query `the beatles` puts *The Beatles* at rank 1 on the production corpus.
3. Query `einstein` keeps *Albert Einstein* at rank 1 (no regression).
4. `ZET_BOOST_TITLE=0` produces ranking equivalent to current behaviour (within score-rounding).
5. Index size grows by less than 5%.
6. Mean query latency increase under 5%.
7. Adding a second field (e.g. caption) requires no further C changes — just a tag registration in psettings_default.c, a wiki2trec.py emission, and a `ZET_BOOST_CAPTION` env var.
