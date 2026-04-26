# PRD-014: Dbkey Passthrough for Wikipedia URLs

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-26

---

## Problem

Result links to en.wikipedia.org are broken for ~23% of articles. Clicking a result for *Wicked (2024 film)* leads to a Wikipedia 404 page rather than the article.

The cause is in `wiki2trec.py`. Wikipedia article titles can contain any printable character — parens, ampersands, colons, periods, accented letters. Zettair's internal document identifier (the `<DOCNO>` tag) goes through Zettair's `mlparse` SGML tokenizer when the index is built, so it must be a single unbroken token. To satisfy this, `wiki2trec.py` runs every title through `safe_id()`:

```python
def safe_id(title):
    return re.sub(r'[^\w\-]', '_', title)[:80]
```

This replaces every non-word character with an underscore. *Wicked (2024 film)* (display form) becomes *Wicked_(2024_film)* (dbkey form, used by the clickstream) becomes *Wicked__2024_film_* (safe_id, written to the index). Three forms of the same title.

The transformation is irreversible. Given `Wicked__2024_film_`, you cannot tell which underscores were originally underscores, parens, ampersands, colons, or other punctuation. So when the frontend builds the link to en.wikipedia.org from the docno that comes back from zet, it produces `https://en.wikipedia.org/wiki/Wicked__2024_film_` — and Wikipedia 404s.

Of 975,796 indexed articles, 230,643 have non-word characters in their dbkey. All of them have wrong links.

---

## Goal

Result links land on the correct Wikipedia article. The frontend builds a URL using the dbkey form (parens preserved), not the safe_id form.

The fix is **localised to `wiki2trec.py` and `server.py`**. No changes to the Zettair index, no reindexing required. No changes to the frontend (it already constructs the URL correctly given a correct docno).

---

## Design

### What stays the same

- Zettair's internal docno remains the safe_id (`Wicked__2024_film_`). Zettair's mlparse tokenizer needs a single unbroken token, and patching the C tokenizer to bypass tokenization for `<DOCNO>` content is too much risk for too little gain.
- The snippets and images sidecar stores remain keyed by safe_id. Those maps were built using the safe_id at build time and the keys travel through unchanged.
- The frontend's `wikiLink()` function is unchanged — it just builds `en.wikipedia.org/wiki/{docno}` from whatever the server returns.

### What changes

When `wiki2trec.py` writes an article, it emits two outputs that depend on the title:

1. `<DOCNO>{safe_id}</DOCNO>` to the TREC file (unchanged).
2. A new line `{safe_id}\t{dbkey}` to a sidecar file `enwiki_top1m.dbkeys.tsv`, **but only when `safe_id != dbkey`**.

For *Albert Einstein* (dbkey `Albert_Einstein`, safe_id `Albert_Einstein`) — they match, no entry written.  
For *Wicked (2024 film)* — emit `Wicked__2024_film_\tWicked_(2024_film)`.

The sidecar file is small (~70 MB for 230k entries) because most titles have only word characters and need no entry.

`server.py` loads the file at startup into a dict `_dbkey_map: dict[str, str]`. In `enrich_results()`, it translates the safe_id docno coming out of zet into the dbkey form using the map (with passthrough for unmapped entries):

```python
docno_raw = r.get("docno", "")                       # safe_id from zet
docno     = _dbkey_map.get(docno_raw, docno_raw)     # dbkey if mapped, else as-is
snippet   = r.get("summary") or _snippets_store.get(docno_raw) or ""
image_url = _images_store.get(docno_raw)             # store lookups still use safe_id
```

The response field `docno` now contains the dbkey. The frontend builds `en.wikipedia.org/wiki/Wicked_(2024_film)` and Wikipedia resolves it normally.

### File format

Plain TSV, one entry per line, no header, sorted by safe_id (for predictable diffs):

```
Wicked__2024_film_	Wicked_(2024_film)
Pushpa_2__The_Rule	Pushpa_2:_The_Rule
Severance__TV_series_	Severance_(TV_series)
```

UTF-8. No escaping — neither field can contain a tab or newline by construction (Wikipedia titles never contain control chars, and `safe_id()` reduces everything to word chars + hyphens).

---

## Memory cost

230,643 entries × ~300 bytes per Python `dict[str, str]` entry ≈ **70 MB resident**.

Current server idle memory: ~670 MB. After: ~740 MB. Well within the 8 GB box budget.

If memory becomes a concern in the future, switch to the `FlatStore` pattern already used for snippets and images: a flat binary store + JSON offset map, with `os.pread()` per lookup. Adds <1 ms per result. Not warranted now.

---

## Bootstrapping the existing index

The current index was built without writing `enwiki_top1m.dbkeys.tsv`. Re-running `wiki2trec.py` would rebuild the entire TREC file (4–8 hours) which is not justified.

A one-shot helper script generates the dbkey map by walking the existing inputs:

- The allowlist `top_titles.txt` already contains the dbkey form of every article that was selected for indexing.
- The TREC file's `<DOCNO>` lines contain the safe_id form.
- For each line `<DOCNO>{safe_id}</DOCNO>` in the TREC file, find the matching dbkey by reversing the mangling against the allowlist:
  - For each allowlist title, compute `safe_id(dbkey)`.
  - Build the inverse map `safe_id → dbkey`.
  - Walk the TREC's docno list and emit `safe_id\tdbkey` only when they differ.

This runs in seconds (the allowlist is 1M lines) and does not touch the index.

The script lives at `wikipedia/build_dbkey_map.py` in the `zettair` repo and is added to `setup.sh` as a new pipeline step.

---

## API Changes

### `/search` response

The `docno` field in result objects now contains the dbkey form. For pre-existing single-word titles like *Albert_Einstein* the response is byte-identical to before. For multi-word titles with punctuation the response now matches Wikipedia's URL format.

Before:
```json
{"rank": 1, "docno": "Wicked__2024_film_", "score": 9.82, ...}
```

After:
```json
{"rank": 1, "docno": "Wicked_(2024_film)", "score": 9.82, ...}
```

### `/click` request body

Clients send `docno` back in click events. The frontend's `docno` is whatever the server gave it, so click logs now record dbkey form. This is the correct user-facing identifier. No frontend change.

### Logs

Query and click logs now contain dbkey docnos. Old log entries with safe_id docnos remain valid (they are what was returned at the time).

---

## Files Changed

| File | Repo | Change |
|---|---|---|
| `wikipedia/wiki2trec.py` | zettair | Emit `enwiki_top1m.dbkeys.tsv` sidecar alongside the TREC file |
| `wikipedia/build_dbkey_map.py` | zettair | New — one-shot script to generate dbkeys for an already-built index |
| `server.py` | zettair-search | Load `_dbkey_map` at startup; translate safe_id → dbkey in `enrich_results()` |
| `deploy/zettair-search.service` | zettair-search | Add `ZET_DBKEYS` environment variable |
| `deploy/setup.sh` | zettair-search | Add a build step that calls `build_dbkey_map.py` for fresh installs |

---

## Implementation Order

1. Add `build_dbkey_map.py` to the `zettair` repo (one-shot script).
2. Run it on the server to produce `/mnt/wikipedia-source/enwiki_top1m.dbkeys.tsv`.
3. Modify `server.py` to load and use the map. Add `ZET_DBKEYS` to the service file.
4. Push, deploy. Test that *Wicked (2024 film)* now links correctly.
5. Modify `wiki2trec.py` so future rebuilds produce the sidecar natively.
6. Add the new step to `setup.sh` so fresh installs build the map automatically.

---

## Success Criteria

1. Searching for *Wicked* on zettair.io and clicking the *(2024 film)* result lands on `https://en.wikipedia.org/wiki/Wicked_(2024_film)` (no Wikipedia 404).
2. Spot-check 10 articles with non-word characters in the title (parens, ampersands, colons, accented letters): all link correctly.
3. Server memory increase under 100 MB at startup.
4. No measurable change in query latency (p50/p95/p99).
5. Click log entries contain dbkey form, not safe_id form.

---

## What This Doesn't Solve

- The 24k articles genuinely missing from the corpus (deleted, renamed, or post-dump-date) still don't appear in results. Out of scope.
- The mangling still happens internally — the index, snippets store, and images store all key on safe_id. This is invisible externally and only matters if a future feature needs to round-trip a docno through the system.
- If a future Wikipedia title contains a literal tab character (which is currently impossible), the TSV format would break. Not worth defending against.
