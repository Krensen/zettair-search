# PRD-019: Per-Field BM25 — Separate Length Norm and IDF Per Field

**Status:** Live on prod (zettair commits `f397601` M1+M2, `f4815b6` M3). Per-field BM25 is now active across OR, AND, and thresh decode paths. `ZET_PERFIELD_BM25=1` is the default. Per-doc field lengths still live in a sidecar — folding into the docmap is the only remaining "should do" and is captured under "TODO: fold sidecars into the docmap" below. M4 (remove per-occurrence boost) and M5 (per-field IDF) are deferred.
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

The docmap entry currently stores one `nwords` per doc (the whole-doc word count). We need per-field length. Two real options:

1. **Extend `docmap_entry`** to carry `field_words[POSTINGS_MAX_FIELDS]`. The natural place — `nwords` already lives there, per-field word counts are conceptually identical, and `makeindex.c` already knows the field_id of each posting. Costs: docmap format bump (existing indexes need rebuilding), and ~95 MB added to the docmap (64 bytes/doc × 1.5M docs).
2. **Sidecar file**: `field_lengths.bin` — `(N_docs × N_fields × uint32)` mmap'd at startup, indexed by `(docno, field_id)`. Same total size on disk, but doesn't touch the docmap format. Loaded only when `ZET_PERFIELD_BM25=1`.

**v1 ships sidecar (option 2).** The honest reason is sequencing: it's the smallest local change to validate the BM25F math without committing to a docmap format migration. The sidecar is gated by `ZET_PERFIELD_BM25=1` so it's invisible to the default scoring path; existing indexes work unchanged.

This is acknowledged technical debt — see "TODO: fold sidecars into the docmap" below. The sidecar should not survive M3.

Layout details: dense `uint32` per (doc, field) cell, zero = "no occurrences in this field". Most docs have only body + title populated; cells for unused fields are simply zero, no special encoding needed. Random-access by `(docno, field_id)` is O(1).

### Per-field corpus statistics

Two new arrays loaded at startup:

```c
double avg_L[POSTINGS_MAX_FIELDS];     // mean field length across docs that have it
unsigned int N_field[POSTINGS_MAX_FIELDS];  // number of docs that contain field f at all
```

v1 ships these in a small `field_stats.bin` companion to the field-lengths sidecar. M3 should fold both into `struct docmap.agg` (which already carries `avg_words`, `sum_words`, `avg_dwords`, etc.) — same sidecar caveat as above.

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

1. **M1 — sidecar + corpus stats** ✅ *(zettair `f397601`, since superseded by `9922923` which moves sidecar generation into the indexer rather than a separate Python script)*: Sidecars are now written by `zet -i` directly, in the same loop that assigns docids — eliminating the docno-misalignment risk that motivated this whole rework.

2. **M2 — per-field BM25 in `or_decode_offsets`** ✅ *(zettair `f397601`)*: OR path accumulates `f_dt_f[]` and computes the per-field sum via `perfield_score()`. Gated by `ZET_PERFIELD_BM25=1`.

3. **M3 — extend per-field BM25 to AND and thresh decode paths** ✅ *(zettair `f4815b6`)*: Mirror the M2 changes into `and_decode_offsets` and `thresh_decode_offsets`. High-frequency queries (`london`, `denver`) that overflow the accumulator limit now go through per-field scoring instead of falling back to the legacy per-occurrence boost. Caveat: the first attempt (`7867f94`) appeared to crash prod, but the actual cause was an unrelated stale-`libzet.so` symlink in `/usr/local/lib/` that masked freshly-built symbols. Once that was fixed in setup.sh (`b5a41c3`), M3 redeployed cleanly and is live. The originally-planned "fold sidecars into the docmap" portion of M3 is split out below — it remains TODO.

4. **M3a — fold sidecars into the docmap (TODO)**: Move `field_words[POSTINGS_MAX_FIELDS]` into `struct docmap_entry` and `avg_field_words[]` / `n_with_field[]` into `struct docmap.agg`. Bump on-disk docmap format. Eliminates the sidecar dance entirely and makes per-field stats travel with the index.
   - Move `field_words[POSTINGS_MAX_FIELDS]` into `struct docmap_entry`. Move `avg_field_words[]` and `n_with_field[]` into `struct docmap.agg`. Bump the on-disk docmap format. Update `makeindex.c` to write per-field word counts as it parses each doc — the field_id is already known at that point thanks to PRD-017.
   - Apply the same `f_dt_f[]` refactor to `and_decode_offsets` and `thresh_decode_offsets`. Replace `g_field_lengths[]` reads with `DOCMAP_GET_FIELD_WORDS(map, docno, field_id)`.
   - Delete `build_field_lengths.py`, the setup.sh sidecar build step, and the `ZET_FIELD_LENGTHS_PATH` / `ZET_FIELD_STATS_PATH` env vars.

5. **M4 — remove per-occurrence boost (deferred)**: Delete `g_field_boost[]`, fold env-var parsing into per-field arrays. Single source of truth. Currently low priority: the legacy path is dead code once `ZET_PERFIELD_BM25=1` is the default (which it is). Removing it cleans the codebase but doesn't change behaviour.

6. **M5 — per-field IDF (deferred, optional)**: Add the on-the-fly per-field doc-frequency accumulation. Use it when `ZET_FIELD_IDF=on`.

7. **M6 — production rollout** ✅: PRD-019 is live on prod with `ZET_PERFIELD_BM25=1`, `ZET_FIELD_W_TITLE=10.0`, `ZET_FIELD_B_TITLE=1.0`, `ZET_FIELD_W_BODY=1.0`, `ZET_FIELD_B_BODY=0.0`. Verified queries: morrissey, mark zuckerberg, denver, london, java, photosynthesis, einstein, indonesia, manchester, facebook, boxing, denver broncos all produce the canonical article at rank 1 with healthy score gaps to runners-up.

M1 and M2 are done; M3 is the format migration that retires the sidecars.

---

## TODO: fold sidecars into the docmap

The current sidecar (`field_lengths.bin` + `field_stats.bin`) is a deliberate v1 simplification. It works, but it has the wrong shape for permanent code:

- **Per-field word counts are intrinsic to the index**, in the same way that whole-doc word count is. They're written once at index-build time and read at every query. They have no business being a separate file — the docmap is exactly where "per-doc statistics computed at index time" lives.
- **`docmap_entry` already has `unsigned int words`**. Adding `unsigned int field_words[POSTINGS_MAX_FIELDS]` alongside it is the obvious extension.
- **`docmap.agg` already has `avg_words`, `sum_words`, `avg_dwords`, etc.** Adding `avg_field_words[POSTINGS_MAX_FIELDS]` and `n_with_field[POSTINGS_MAX_FIELDS]` is the obvious extension.
- **Sidecars create a coherence hazard**: rebuild the index without rebuilding the sidecar, and the sidecar's docno alignment silently breaks. We've already lived through this once with `click_prior.bin` (PRD-006), where stale data went undetected for weeks. Putting the per-field lengths in the docmap means they travel with the index — impossible to get out of sync.

What stops us doing this right now is that bumping the docmap on-disk format means existing indexes need rebuilding. We're going to rebuild anyway when we add caption/category/infobox tags to `wiki2trec.py`, so M3 is the natural place to bundle the docmap migration with the AND/thresh refactor. Doing both at once means we only force a reindex once.

When this happens, delete:
- `wikipedia/build_field_lengths.py`
- `field_lengths.bin` and `field_stats.bin` from the volume layout
- `okapi_load_perfield()` and the `g_field_lengths` / `g_field_avg_len` / `g_field_n_with` globals in `okapi.c`
- `ZET_FIELD_LENGTHS_PATH` and `ZET_FIELD_STATS_PATH` env vars
- The setup.sh sidecar build step (12a)

Replace with `DOCMAP_GET_FIELD_WORDS(map, docno, field_id)` accessor and `docmap_avg_field_words(map, field_id)` for the corpus stats.

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
