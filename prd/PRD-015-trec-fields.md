# PRD-015: First-Class TITLE and URL Fields in TREC Documents

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-27

---

## Problem

PRD-014 fixed the broken Wikipedia links by introducing a sidecar file (`enwiki_top1m.dbkeys.tsv`) that maps the mangled `safe_id` form back to the canonical dbkey form. It works, but it's a workaround on top of a workaround:

1. `wiki2trec.py` runs every title through `safe_id()` to keep Zettair's `mlparse` tokenizer happy. This is irreversible mangling.
2. `enwiki_top1m.dbkeys.tsv` is a separate file shipped alongside the index, loaded into RAM at server start, and consulted on every result to undo the damage.

The fundamental problem is that we treat Zettair's docno as the human-facing identifier for the article. It isn't — it's an internal index key with awkward syntactic constraints. The dbkey (`Wicked_(2024_film)`) is the correct identifier for the user. The URL (`https://en.wikipedia.org/wiki/Wicked_(2024_film)`) is the correct hyperlink. Neither belongs in the docno field.

When we eventually want to surface other per-document data (the article title in display form, the Wikipedia URL, the canonical Wikidata Q-ID, the article's primary image hint), the sidecar pattern multiplies. Each new field becomes another file to load at startup and another lookup in `enrich_results()`.

---

## Goal

Store the canonical title and URL **inside the TREC document**, alongside the text, in fields that Zettair's C summariser can read at query time and emit in its JSON output. The server passes them through to the response. No sidecar files. No startup map loads. No in-memory translation table.

When this is done, the dbkey sidecar file (`enwiki_top1m.dbkeys.tsv`) and all the code that reads it can be deleted.

---

## Design

### TREC document format

Currently `wiki2trec.py` emits:

```
<DOC>
<DOCNO>Wicked__2024_film_</DOCNO>
<TEXT>
Wicked (2024 film). Wicked is a 2024 American musical fantasy film...
</TEXT>
</DOC>
```

After this PRD:

```
<DOC>
<DOCNO>Wicked__2024_film_</DOCNO>
<TITLE>Wicked (2024 film)</TITLE>
<URL>https://en.wikipedia.org/wiki/Wicked_(2024_film)</URL>
<TEXT>
Wicked is a 2024 American musical fantasy film...
</TEXT>
</DOC>
```

The `<DOCNO>` tag remains the safe_id form (Zettair's tokenizer requires this). The `<TITLE>` and `<URL>` fields are stored verbatim — Zettair's `mlparse` parser treats their contents as document text by default, but we'll bypass that for these specific fields (see "Zettair changes" below).

The `<TEXT>` field no longer needs the `Title. ` prefix that wiki2trec currently puts there for searchability — having the title indexed inside `<TEXT>` is exactly what we want for query matching. We keep that prefix as-is.

### Zettair changes

Zettair already has the docmap, an internal record of one entry per document. The docmap stores the docno, document length, weight, and a few bytes of metadata. We add two new variable-length fields to the docmap entry: `title` and `url`.

The C-side changes:

1. **`wiki2trec.py` writes `<TITLE>` and `<URL>` tags.** Trivially.

2. **Zettair recognises `<TITLE>` and `<URL>` during indexing.** In `makeindex.c`, alongside the existing handling for `<DOCNO>`, add similar state-machine branches for `<TITLE>` and `<URL>`. Their content is captured raw (not tokenized) and stored in the docmap entry for that document. This mirrors how `<DOCNO>` content is collected — it's already a special case that bypasses tokenization for the term index, but currently still gets passed through `mlparse_word`. The new fields skip that, keeping every byte intact.

3. **Docmap stores the new fields.** `docmap.c` and `docmap.h` get two new accessor functions: `docmap_get_title(map, docno)` and `docmap_get_url(map, docno)`. Storage on disk: append the two strings (with length prefixes) to the existing variable-length section of each docmap entry.

4. **JSON output emits the new fields.** `commandline.c` (the same file we patched in PRD-011 to add `summary` to the JSON) reads the title and URL from the docmap and includes them in the per-result JSON line:

```json
{"rank":1,"docno":"Wicked__2024_film_","title":"Wicked (2024 film)","url":"https://en.wikipedia.org/wiki/Wicked_(2024_film)","score":9.82,"docid":12345,"summary":"..."}
```

The size impact on the docmap is real but bounded: 1M articles × ~80 bytes per (title + url) ≈ 80 MB extra. The existing docmap is ~1 MB so this is a real percentage increase, but the total is still trivial.

### Server changes

`server.py` becomes simpler:

```python
def enrich_results(results: list) -> list:
    enriched = []
    for r in results:
        docno = r.get("docno", "")
        snippet = r.get("summary") or _snippets_store.get(docno) or ""
        enriched.append({
            "rank": r["rank"],
            "score": r["score"],
            "docid": r["docid"],
            "docno": docno,
            "title": r.get("title", docno.replace("_", " ")),
            "url": r.get("url", f"https://en.wikipedia.org/wiki/{docno}"),
            "snippet": snippet,
            "image_url": _images_store.get(docno),
        })
    return enriched
```

The fallbacks (`docno.replace("_", " ")` for title; constructing the URL from the docno) handle the case where the new fields are absent, which lets us deploy the server change before the C changes are merged without breaking anything. Once the C changes ship, the fallbacks become dead code we can remove later.

### Frontend changes

`index.html` builds the wiki link from `r.docno`. After this PRD it builds it from `r.url` directly. Single line change.

The display title shown in result cards currently comes from `r.docno.replace(/_/g, ' ')`. After this PRD it comes from `r.title`. Two lines.

---

## Removing the PRD-014 hack

Once the new fields are in place and verified:

### Files to delete

| File | Repo |
|---|---|
| `wikipedia/build_dbkey_map.py` | zettair |
| `/mnt/wikipedia-source/enwiki_top1m.dbkeys.tsv` | (server volume) |
| `prd/PRD-014-dbkey-passthrough.md` | zettair-search (or mark superseded) |

### Code to delete

In `server.py`:

- Global `_dbkey_map` declaration
- `DBKEYS_PATH` constant  
- `_load_dbkey_map()` function
- The `_load_dbkey_map()` call in `lifespan()`
- The `_dbkey_map.get(docno_raw, docno_raw)` translation in `enrich_results()` (replaced by reading `r["url"]` directly)

In `deploy/zettair-search.service`:

- `Environment=ZET_DBKEYS=...`

In `deploy/setup.sh`:

- The "Building dbkey map" step that calls `build_dbkey_map.py`

In `wikipedia/wiki2trec.py`:

- The `dbkeys_path` variable
- Opening the `dbkeys` file handle
- The `dbkey_remap` counter and the line writing `safe_id\tdbkey` per article
- The closing `, {dbkey_remap:,} dbkey remaps` in the final summary line

PRD-014 marked **Superseded by PRD-015** at the top.

---

## Why this is correct now even though it was wrong then

PRD-014 was the right call at the time. The index existed. Replacing it would have meant 4–8 hours of TREC regeneration plus 30–60 minutes of indexing on the live server. The sidecar shipped in 30 minutes and unblocked the broken links.

PRD-015 is the right call now because:

1. The site has no users, so a rebuild has no cost beyond machine time.
2. The next quarterly corpus rebuild will happen anyway — folding this in costs nothing extra.
3. Every additional sidecar makes the system harder to reason about. Killing one *now* (when it's the only one) prevents the pattern from spreading.
4. Future per-article fields (Wikidata Q-ID, primary category, language links) become trivial to add — just another tag in the TREC and another field in the docmap.

---

## Risks

1. **Touching `makeindex.c` and `docmap.c` is the most invasive C work to date.** Previous patches (`okapi.c` for click prior, `commandline.c` for JSON output) were narrowly scoped. This patch changes the on-disk docmap format. Backwards incompatibility with previously-built indexes is acceptable (we're rebuilding anyway) but the test suite must pass and any of Zettair's own internal tools (`zet -d`, `zet -e`) must still work.

2. **Index size grows.** ~80 MB extra in the docmap on disk and resident at runtime. Trivial in absolute terms but the docmap currently fits in a couple of mmap pages — no longer.

3. **Reindex required.** The whole pipeline (`select_top_articles.py`, `wiki2trec.py`, `zet -i`, `build_docno_map.py`, `build_click_prior.py`, `build_autosuggest.py`, `build_docstore.py`) must be re-run. ~6–10 hours wall time. The existing index can stay live during the rebuild and we cut over with a service restart, exactly like PRD-012.

---

## Files Changed

| File | Repo | Change |
|---|---|---|
| `wikipedia/wiki2trec.py` | zettair | Emit `<TITLE>` and `<URL>` tags; remove dbkeys.tsv writing |
| `devel/src/makeindex.c` | zettair | Recognise `<TITLE>` and `<URL>` tags; capture content raw |
| `devel/src/docmap.c`, `docmap.h` | zettair | Store and retrieve title and url per document |
| `devel/src/commandline.c` | zettair | Emit title and url in JSON Lines output |
| `wikipedia/build_dbkey_map.py` | zettair | Delete |
| `server.py` | zettair-search | Read `title` and `url` from result; remove dbkey map loading |
| `index.html` | zettair-search | Use `r.url` and `r.title` directly |
| `deploy/zettair-search.service` | zettair-search | Remove `ZET_DBKEYS` |
| `deploy/setup.sh` | zettair-search | Remove dbkey map build step |

---

## Implementation Order

1. Patch `makeindex.c` and `docmap.c` to handle the new tags. Build, run Zettair's test suite, verify with a small TREC file containing TITLE and URL fields.
2. Patch `commandline.c` to emit the fields in JSON. Test with `echo 'einstein' | zet ... --output=json`.
3. Modify `wiki2trec.py` to emit the new tags (keeping the dbkeys.tsv write for now so the live server isn't broken).
4. Modify `server.py` to read `title` and `url` from results with fallbacks. Deploy. Old index still works because of the fallbacks.
5. Trigger a full corpus rebuild (`select_top_articles.py` → `wiki2trec.py` → `zet -i` → ...). Cut over to the new index.
6. Verify the new fields are present and the URLs work for articles with punctuation.
7. Remove the PRD-014 hack: delete `_dbkey_map` plumbing from `server.py`, drop `ZET_DBKEYS` from the service file, drop the dbkey step from `setup.sh`, delete `build_dbkey_map.py`, update `wiki2trec.py` to stop writing the sidecar, mark PRD-014 superseded.

---

## Success Criteria

1. The TREC file produced by `wiki2trec.py` contains `<TITLE>` and `<URL>` tags for every article.
2. `zet --output=json` emits `title` and `url` fields on every result line.
3. Searching for "wicked" on zettair.io and clicking the *(2024 film)* result lands on the correct Wikipedia article — same outcome as PRD-014, but achieved without a sidecar.
4. `_dbkey_map` no longer exists in `server.py`. `ZET_DBKEYS` no longer exists in the service file.
5. `enwiki_top1m.dbkeys.tsv` is no longer present on the volume after the next setup run.
6. Server startup memory usage drops by ~70 MB.
