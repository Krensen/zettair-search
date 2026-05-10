# PRD-019: Per-Field BM25 — Separate Length Norm and IDF Per Field

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-10

---

## Problem

PRD-017 added a per-field boost — title hits count as `g_field_boost[TITLE] × 1` body hits in the term-frequency contribution. That's a per-occurrence multiplier, not a real field-aware ranker. It blends every field into one BM25 calculation that uses:

- **Whole-doc length** for length normalisation (no notion of title length vs body length).
- **Whole-corpus IDF** for every term (no notion of "this term is rare in titles, common in bodies").

Concrete failure cases on the production index:

- Query `morrissey`: top 10 has `David_Morrissey`, `Neil_Morrissey`, `John_Morrissey` all tied with the canonical `Morrissey` article on title contribution because each has a 1-occurrence title hit. The canonical article gets buried below several derivative articles.
- Query `mark zuckerberg`: lost to `Randi_Zuckerberg` (her brother's article) because Mark's article is long and Randi's body is densely packed with the phrase.
- Query `morrissey` again: `List_of_songs_recorded_by_Morrissey` (6-word title with one matching word) ties with `Morrissey` (1-word title fully filled by query) on title contribution. They should be very different signals.

The root cause is that **title length matters and we ignore it**. A query that fills 100% of the title is a much stronger signal than one that fills 17%, but BM25 doesn't see this because the length normalisation is on combined doc length and the per-occurrence boost is a flat multiplier.

The same logic applies to other natural fields — captions, infoboxes, see-also, categories. PRD-017 reserved 16 field IDs in the posting offset (4 bits) precisely to allow per-field treatment, but only the title boost is wired through, and it's wired through as a multiplier rather than as a real per-field BM25.

---

## Goal

Generalise to **proper BM25F across N fields** (up to 16, matching the 4-bit field tag in posting offsets):

```
score(d, q) = sum_t  IDF_field(t)  *  sum_f  w_f * BM25_tf(f_dt_f, L_f, avg_L_f, k1, b_f)
            + click_addend(d)
```

where:

- `f` ranges over the active fields (body, title, caption, …).
- `f_dt_f` = occurrences of term `t` in field `f` of doc `d`.
- `L_f` = length (in words) of field `f` in doc `d`.
- `avg_L_f` = corpus-mean length of field `f`.
- `b_f` = length-norm parameter for field `f`. Title likely wants `b ≈ 1.0` (the more of the title the query covers, the bigger the signal); body wants something moderate (`b ≈ 0.5` say).
- `w_f` = field weight. Generalises today's `g_field_boost`. Body defaults to 1.0; titles to 3–5.
- `IDF_field(t)` is the per-field IDF — `log((N - f_t_field + 0.5) / (f_t_field + 0.5))`. Optional refinement; could fall back to corpus-wide IDF if a per-field doc-frequency isn't tracked for that field.

When this is done:

- Query `morrissey` ranks the canonical `Morrissey` article ahead of `David_Morrissey` because its title is 100% filled by the query, while David's title is 50% filled.
- Adding "infobox" or "caption" as a field is one diff — register the field ID in `psettings`, set its `w_f` and `b_f`, write field-length-N at index time. No retrofitting per-field of okapi.c.
- The 16-field budget from PRD-017 is fully usable.
- Per-occurrence `g_field_boost[]` (the today implementation) is removed.

---

## Why one index, not N indexes

A natural alternative is "one `.v` file per field". Each field becomes a vanilla BM25 query with its own length norm and IDF; the runtime queries each, then combines on docno. Considered and rejected:

- **Storage**: ~Nx postings volume. Each occurrence is in a separate posting list with its own dictionary term. Today's index already encodes field_id in 4 bits; we lose that compression entirely.
- **Query cost**: N decode passes per term per query. A single-field index already dominates query latency on common terms; multiplying it by 4–6 fields is not a free choice.
- **Doc-ID alignment**: needs a docno join across N indexes, with consequences for the merge in `index_heap_select`. Adds a real algorithm where today there is none.
- **Adding a field is harder, not easier**: every new field is an extra index to build, deploy, sidecar, and version. With one index, it's a `psettings` registration plus an env var.
- **Doesn't actually beat single-index per-field BM25 on quality**: the math is identical. The only thing the separate-index approach buys is conceptual cleanliness, which we don't need.

Single index with per-field statistics is strictly less work for the same ranking quality.

---

## Design

### Posting format

Unchanged. Each posting offset already encodes the field_id in the low 4 bits (PRD-017). We continue to read the field_id during the per-occurrence loop. The decode path doesn't change shape — it just accumulates into N separate `f_dt_f` counters instead of one `weighted_f_dt`.

### Per-doc field lengths

The docmap entry currently stores one `nwords` per doc (the whole-doc word count). We need per-field length. Two options:

1. **Extend `docmap_entry`** to carry an array of `field_lengths[POSTINGS_MAX_FIELDS]`. Simple but bloats the docmap by 64 bytes/doc (16 × `uint32`). At 1.5M docs that's ~95 MB.
2. **Sidecar file**: `field_lengths.bin` — `(N_docs × N_fields × uint32)` mmap'd at startup, indexed by `(docno, field_id)`. Same total size on disk but doesn't touch the docmap format.

Recommend (2). Rationale: docmap is a hot, small, frequently-accessed structure; keeping it tight matters. A sidecar is an mmap, accessed only during scoring, can be loaded lazily, and doesn't force a docmap format bump every time we add a field type.

The sidecar uses one `uint32` per (doc, field) cell; zero is "no occurrences in this field" (the field is absent for that doc). Most docs have only body + title populated; cells for unused fields are simply zero, no special encoding needed. With 16 fields × 1.5M docs × 4 bytes = ~95 MB. Storage is fine.

Actually — many fields will be absent from many docs, so the dense layout wastes ~85% of cells. Worth considering a **sparse layout** keyed on (docno, field_id) → length. But the dense layout has random-access in O(1), which matters at score time when we're hitting it once per posting per query term. Stick with dense for v1; revisit if size becomes a problem.

### Per-field corpus statistics

Two new arrays loaded at startup:

```c
double avg_L[POSTINGS_MAX_FIELDS];     // mean field length across docs that have it
unsigned int N_field[POSTINGS_MAX_FIELDS];  // number of docs that contain field f at all
```

Computed once at index-build time and written into a small header file (`field_stats.bin`) alongside the field-lengths sidecar.

### Per-field IDF

For each query term `t` and each field `f`, `f_t_f` = number of docs where `t` appears at least once in field `f`. Today the postings dictionary tracks one `f_t` per term. We need either:

- **Per-field `f_t`**: extend the dict entry to track `f_t_f` separately. More format change.
- **Compute on the fly during the term scan**: walk postings, count distinct (doc, field) pairs. Cheap during the per-occurrence loop we already do, since we already know the field_id of each posting.

Recommend the on-the-fly approach for v1. We're walking the postings anyway. Keeps the index format unchanged.

### Score formula

In okapi.c, the per-term inner loop (`or_decode_offsets`, `and_decode_offsets`, `thresh_decode_offsets`) currently computes one `weighted_f_dt`. Replace with `f_dt_f[NUM_FIELDS]`, one counter per field. After the posting walk completes for a doc:

```c
double score_contribution = 0.0;
for (int f = 0; f < num_fields_active; f++) {
    if (f_dt_f[f] == 0) continue;
    double L_f      = field_length(docno, f);
    double bm25_tf  = ((k1 + 1) * f_dt_f[f]) /
                      (k1 * ((1 - b_f[f]) + b_f[f] * L_f / avg_L[f]) + f_dt_f[f]);
    score_contribution += w_f[f] * bm25_tf;
}
score_contribution *= idf_field(t, ?) * r_qt;
acc->acc.weight += score_contribution;
```

For the per-field IDF, we have to make a call: weighted average of per-field IDFs by field weight, or use the corpus-wide IDF as today. v1 uses corpus-wide IDF; per-field IDF can be a follow-up flag.

### Configuration (env vars)

Generalises today's `ZET_BOOST_TITLE` to per-field `w_f` and per-field `b_f`:

```
ZET_FIELD_W_BODY     = 1.0       # weight for body (default)
ZET_FIELD_W_TITLE    = 3.0       # weight for title
ZET_FIELD_W_CAPTION  = 1.5
ZET_FIELD_W_CATEGORY = 1.0
ZET_FIELD_W_SEEALSO  = 1.0
ZET_FIELD_W_INFOBOX  = 1.5

ZET_FIELD_B_BODY     = 0.5
ZET_FIELD_B_TITLE    = 1.0
ZET_FIELD_B_CAPTION  = 0.75
ZET_FIELD_B_CATEGORY = 0.75
ZET_FIELD_B_SEEALSO  = 0.75
ZET_FIELD_B_INFOBOX  = 0.75
```

Backwards compat: read `ZET_BOOST_TITLE` as `ZET_FIELD_W_TITLE` if set. Document the new names; deprecate but don't remove the old in the same release.

### Removing the per-occurrence boost

`g_field_boost[]` and the per-occurrence weighted-f_dt path go away once the new per-field accumulation is correct. The fast-path code (`g_field_boost_active = 0` → use scan path) still applies — when only body has nonzero `w_f`, we skip per-field reading and compute single-field BM25 like today.

---

## Migration

The index format itself is unchanged (postings still carry field_id in offset bits). What changes:

- New sidecar `field_lengths.bin` written at index-build time.
- New sidecar `field_stats.bin` (just `avg_L[]` and `N_field[]`).
- Both built once during reindex; loaded mmap at startup.

A reindex is required to produce the sidecars. We've been doing reindexes anyway when we change fields (e.g., adding TITLE). PRD-017 documented the same constraint.

For the production server: during the reindex pipeline (already invoked when refreshing the corpus), build the sidecars from the same TREC pass. They're cheap to compute compared to the index build itself — one counter per (doc, field).

---

## Open questions

1. **Per-field IDF or shared IDF**: v1 punts and uses corpus-wide IDF. A query for "morrissey" gets the same IDF whether the term occurs in title or body. This is sub-optimal but a reasonable starting point. Ship a follow-up flag (`ZET_FIELD_IDF=on/off`) once the basic per-field BM25 is in.

2. **Default `b_title`**: 1.0 is full normalisation. Sounds right intuitively (a 1-word title fully filled by query is huge signal) but might over-penalise legitimate compound-title queries like "battle of trafalgar" finding `Battle_of_Trafalgar`. Tune with the test set.

3. **What happens to docs with no title at all** (e.g., redirect stubs that slip through): `f_dt_title=0` for that doc, contributes 0 to the title term — same as having a body-only doc. No special handling required.

4. **Sidecar size at corpus refresh**: 95 MB for 16 fields × 1.5M docs is fine on the VPS. But if we expand the corpus to 5M docs the sidecar grows to ~320 MB. Worth thinking about a sparse layout before the next corpus expansion.

5. **What if `psettings` registers a 17th field type one day?** PRD-017 reserved 16 IDs (4 bits). PRD-019 inherits that limit. Going beyond requires bumping the field-bits budget in the posting offset, which is a real format change. Out of scope for now.

---

## Milestones

1. **M1 — sidecar + corpus stats**: Write `field_lengths.bin` + `field_stats.bin` at index-build time. Load both at startup. No score changes yet — verify the per-field length data is correct by spot-checking a few articles.

2. **M2 — per-field BM25 in `or_decode_offsets`**: Refactor the OR path to accumulate `f_dt_f[]` and compute the per-field sum. Run side-by-side with the per-occurrence-boost path; gate behind a flag. Compare scores on the test set.

3. **M3 — extend to AND and thresh paths**: Apply the same refactor to `and_decode_offsets` and `thresh_decode_offsets`. (These paths handle multi-term queries with conjunctions.)

4. **M4 — remove per-occurrence boost**: Delete `g_field_boost[]`, fold env-var parsing into per-field arrays. Single source of truth.

5. **M5 — per-field IDF (optional)**: Add the on-the-fly per-field doc-frequency accumulation. Use it when `ZET_FIELD_IDF=on`.

6. **M6 — production rollout**: Trigger reindex with new sidecars. Update systemd unit env vars. Verify Morrissey, Mark Zuckerberg, and other previously-broken queries.

M1 and M2 are the riskiest; once those are correct, the rest is mechanical.

---

## Success criteria

- Query `morrissey` ranks `Morrissey` (canonical article) at rank 1 or 2.
- Query `mark zuckerberg` ranks `Mark_Zuckerberg` at rank 1.
- Query `denver` ranks `Denver` at rank 1, ahead of `Denver_Broncos` (currently the broncos win).
- p95 search latency unchanged or within 10% of today.
- Adding a new field type (e.g., infobox, caption) is one psettings registration + one env var, no okapi.c diff.

---

## Non-decisions deferred

- Whether to track per-field doc-frequencies in the dictionary (vs. on-the-fly compute): on-the-fly for v1.
- Whether to support per-field BM25 with sparse field-length storage: dense for v1.
- Whether to expose per-query field-weight overrides in the search request (e.g., `?w_title=10`): no — env-only, restart to retune.
- Whether to drop click prior into per-field too: no — click prior stays as a single additive nudge applied in `post()`.
