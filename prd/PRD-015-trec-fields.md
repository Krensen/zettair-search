# PRD-015: Disk-Resident Title and URL Stores

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-27

---

## Problem

PRD-014 fixed the broken Wikipedia links by introducing an in-memory map (`enwiki_top1m.dbkeys.tsv`) that translates Zettair's mangled `safe_id` form back to the canonical dbkey form at query time. It works, but the part that's wrong is **how it's stored**: ~70 MB loaded into a Python `dict` at server startup.

That's the only data on the entire server that's loaded eagerly into RAM beyond the small autosuggest list. Snippets, images, and the docstore are all on disk, with only their offset maps in memory, and seek-per-lookup at query time. The dbkey map is the odd one out.

There's also a second-order problem: PRD-014 only ships the dbkey, not the canonical URL. The frontend constructs the URL by string concatenation: `https://en.wikipedia.org/wiki/${docno}`. That works for English Wikipedia today but couples the data layer to the URL format. If we ever index a different source (Simple English again? Wiktionary? something non-Wikipedia?), the URL format becomes another piece of code to change.

---

## Goal

Replace the in-memory dbkey dict with a disk-resident store using the same `FlatStore` pattern that already serves snippets and images. Store the **canonical URL** (not just the dbkey) so the frontend doesn't construct it. Drop the `_dbkey_map` plumbing entirely.

When this is done, server memory drops by ~70 MB at startup, and articles with punctuation in their titles link correctly without any startup-loaded translation table.

---

## Design

### What changes in the indexing pipeline

`wiki2trec.py` already writes two FlatStore pairs as it processes each article:

- `enwiki_top1m_snippets.store` + `.map` — snippet text, keyed by safe_id
- `enwiki_top1m_images.store` + `.map` — Wikimedia image URL, keyed by safe_id

We add a third pair, written at the same time, in the same loop, with the same pattern:

- `enwiki_top1m_urls.store` + `.map` — Wikipedia article URL, keyed by safe_id

The store contains the URL string. The map records `{safe_id: [offset, length]}`. Total size: ~50 MB store + ~50 MB map for 1M articles. The map (the only thing in RAM) is the same size as the snippets map — which is already on the volume and well within the existing memory envelope.

For each article, the URL written is:

```
https://en.wikipedia.org/wiki/{dbkey}
```

where `dbkey` is `title.replace(' ', '_')` (parens preserved, all punctuation preserved).

We do **not** ship a separate "title" store. The display-form title is derived trivially from the URL by URL-decoding the path component if needed and replacing underscores with spaces — but in practice, the frontend already does this from the dbkey portion of the URL. One file does both jobs.

### What changes in the server

`server.py` gets a third `FlatStore` instance:

```python
_urls_store = FlatStore(URLS_STORE_PATH, URLS_MAP_PATH, "urls")
```

In `enrich_results()`, it replaces the `_dbkey_map` lookup with a `_urls_store.get()` call:

```python
def enrich_results(results: list) -> list:
    enriched = []
    for r in results:
        docno = r.get("docno", "")  # safe_id from zet
        snippet = r.get("summary") or _snippets_store.get(docno) or ""
        url = _urls_store.get(docno) or f"https://en.wikipedia.org/wiki/{docno}"
        enriched.append({
            "rank": r["rank"],
            "score": r["score"],
            "docid": r["docid"],
            "docno": docno,
            "url": url,
            "snippet": snippet,
            "image_url": _images_store.get(docno),
        })
    return enriched
```

The `docno` field in the response now contains the safe_id again (as it did before PRD-014), but the frontend doesn't use it to build the URL — it uses the new `url` field directly.

The fallback `f"https://en.wikipedia.org/wiki/{docno}"` covers the case where the URL store is missing or doesn't have an entry — so it degrades gracefully (still 404s on punctuation articles, but works for the 77% that don't need remapping).

### What changes in the frontend

`index.html` currently has:

```javascript
const wikiLink = docno => `https://en.wikipedia.org/wiki/${encodeURIComponent(docno)}`;
```

Becomes:

```javascript
const wikiLink = r => r.url || `https://en.wikipedia.org/wiki/${encodeURIComponent(r.docno)}`;
```

(Pass the result object instead of just docno, use the URL if present, fall back to the constructed form.)

The display title shown in result cards currently comes from `r.docno.replace(/_/g, ' ')`. We can leave that alone — the docno (safe_id) is good enough for display purposes ("Wicked (2024 film)" displays fine even though the URL needs the dbkey form), or we can derive the display title from `r.url` by extracting the path component and decoding it. The current approach already works visually — the bug was only in the link target. Leave the display untouched.

### Bootstrapping the existing index

The current index was built before this PRD, so the URL store doesn't exist yet. We have two options:

1. **Build it from the existing `enwiki_top1m.dbkeys.tsv`.** The dbkeys file already contains every safe_id ↔ dbkey pair we need. A one-shot script reads it and writes `enwiki_top1m_urls.store` + `.map`. Takes seconds.

2. **Wait until the next corpus rebuild.** PRD-014's sidecar keeps working in the meantime.

Option 1 is the right call — it lets us deploy the new code immediately and delete the PRD-014 plumbing now rather than waiting for a quarterly rebuild.

A small script `wikipedia/build_urls_store.py` reads `enwiki_top1m.dbkeys.tsv` line by line, appends `https://en.wikipedia.org/wiki/{dbkey}` to the `.store` file, and records offsets in the `.map` file. Mirrors the FlatStore pattern in wiki2trec.py exactly.

For docnos where safe_id == dbkey (no remapping needed), we can either:
- **Skip them** (saves 75% of the store size). server.py's `_urls_store.get()` returns None for these, the fallback `f"https://en.wikipedia.org/wiki/{docno}"` handles them correctly.
- **Include them** (~2× the store size, but consistent semantics — every doc has a URL).

Skip them. Same logic as the dbkeys file, same memory savings, fallback already exists.

---

## Removing the PRD-014 hack

Once the URL store is in place and verified:

### Files to delete

| File | Where |
|---|---|
| `wikipedia/build_dbkey_map.py` | `zettair` repo |
| `/mnt/wikipedia-source/enwiki_top1m.dbkeys.tsv` | server volume |

### Code to delete

In `server.py`:
- Global `_dbkey_map: dict = {}`
- `DBKEYS_PATH` constant
- `_load_dbkey_map()` function
- The `_load_dbkey_map()` call in `lifespan()`
- The `_dbkey_map.get(docno_raw, docno_raw)` translation in `enrich_results()` (already replaced by the URL store lookup)

In `deploy/zettair-search.service`:
- `Environment=ZET_DBKEYS=...`

In `deploy/setup.sh`:
- The "Building dbkey map" step that calls `build_dbkey_map.py`. Replaced by a step that calls `build_urls_store.py` (or, after PRD-015 lands in `wiki2trec.py`, deleted entirely because future rebuilds produce the URL store directly).

In `wikipedia/wiki2trec.py`:
- The `dbkeys_path` variable, file handle, write-per-article logic, counter, summary line. Replaced by the equivalent logic that writes the URL store.

PRD-014 marked **Superseded by PRD-015** at the top.

---

## Why this is the right shape (and the original PRD-015 wasn't)

The original PRD-015 proposed adding TITLE and URL as first-class fields in Zettair's docmap, requiring patches to `makeindex.c`, `docmap.c`, and `commandline.c`. After reading those files carefully it became clear that:

- The docmap has no schema version, so old indexes would silently misread under the new binary, or vice versa. Adding versioning is itself a refactor.
- Three function signatures change (`docmap_add`, plus two new accessors), and every caller of `docmap_add` updates in lockstep.
- The `psettings` attribute system in `makeindex.c` would either need new attributes added (and the config files updated), or hard-coded tag matching that bypasses the existing pattern.
- Realistic effort: 6–8 hours of careful C work, with subtle correctness risks in the binary encode/decode.

The FlatStore approach uses a pattern that's **already in production for two stores** (snippets, images), takes ~30 minutes of Python work, and produces the same external behaviour. The architectural purity argument for storing this in Zettair itself is real but not worth the cost.

---

## Memory and disk impact

| Resource | Before (PRD-014) | After (PRD-015) | Δ |
|---|---|---|---|
| Server RAM at startup | ~770 MB | ~700 MB | **−70 MB** |
| Volume disk usage | +25 MB (dbkeys.tsv) | +50 MB (urls.store + .map) | +25 MB |
| Per-result query cost | 1 dict lookup | 1 dict lookup + 1 `os.pread` | +<1 μs |

Net: ~70 MB RAM reclaimed at the cost of ~25 MB on disk and a sub-microsecond `pread` per result. Fair trade.

---

## Files Changed

| File | Repo | Change |
|---|---|---|
| `wikipedia/wiki2trec.py` | zettair | Write `_urls.store` + `_urls.map` per article (mirrors snippets/images); stop writing `dbkeys.tsv` |
| `wikipedia/build_urls_store.py` | zettair | New — one-shot bootstrap script for the existing index |
| `wikipedia/build_dbkey_map.py` | zettair | Delete |
| `server.py` | zettair-search | Add `_urls_store` FlatStore; emit `url` in response; remove dbkey map plumbing |
| `index.html` | zettair-search | Use `r.url` for the link target |
| `deploy/zettair-search.service` | zettair-search | Replace `ZET_DBKEYS` with `ZET_URLS_STORE` and `ZET_URLS_MAP` |
| `deploy/setup.sh` | zettair-search | Replace dbkey build step with urls store build step |

---

## Implementation Order

1. Modify `wiki2trec.py` to write the urls store/map alongside snippets and images (for future rebuilds). Stop writing `enwiki_top1m.dbkeys.tsv`.
2. Add `build_urls_store.py` to bootstrap the existing index from `enwiki_top1m.dbkeys.tsv`.
3. Run `build_urls_store.py` on the server.
4. Modify `server.py`: add `_urls_store` FlatStore, emit `url` in response, remove `_dbkey_map` plumbing. Service file: swap env vars.
5. Modify `index.html` to use `r.url`.
6. Deploy. Verify a punctuation article (Wicked (2024 film)) links correctly.
7. Delete `build_dbkey_map.py` and `/mnt/wikipedia-source/enwiki_top1m.dbkeys.tsv`. Mark PRD-014 superseded.

---

## Success Criteria

1. `/search` responses contain a `url` field for every result.
2. Articles with punctuation in their titles link to the correct Wikipedia URL — same outcome as PRD-014, but via disk-resident data.
3. `_dbkey_map` no longer exists in `server.py`. `ZET_DBKEYS` no longer exists in the service file.
4. Server startup memory drops by ~70 MB.
5. Query latency unchanged (within noise — `os.pread` per result is microseconds).
6. `enwiki_top1m.dbkeys.tsv` is no longer present on the volume after the next setup run.
