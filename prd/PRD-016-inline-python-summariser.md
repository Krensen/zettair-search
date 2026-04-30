# PRD-016: Inline Python Summariser

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-29

---

## Problem

The C summariser inside Zettair, wired in by PRD-011, produces visibly worse snippets than the Python summariser it replaced. Concrete failure modes seen on real queries:

- **Junk fragments win**: the C scorer treats bullet-list lines, captions, and citation fragments as eligible sentences. A 5-word caption like `Albert Einstein in 1921` containing a query term scores higher than the actual lead paragraph.
- **Stop words pollute the signal**: the C scorer counts every query term equally, with no IDF and no stopword filtering. For a query like "the beatles" half the score comes from `the`, which appears in nearly every fragment. Real prose paragraphs lose to short fragments full of `the`.
- **No length normalisation**: the score formula is `qterms² / total_query_terms`. A 5-word fragment with 2 hits scores the same as a 200-word paragraph with 2 hits.

The Python summariser handled all three:
- A `_is_prose()` filter that rejected fragments shorter than 30 chars, with less than 45% alpha characters, matching citation patterns, or lacking any verb-like word.
- Stopwords stripped from the query before scoring.
- A `hits / length` density score that gives long-paragraph 1-hit fragments much less weight than short-prose 1-hit fragments.

It also read from the docstore (cleaned text) rather than the raw TREC, so it saw less markup noise to begin with.

We deleted `summarise.py` in commit `0cab5de` because we believed the C summariser was doing the same job. It isn't.

---

## Goal

Restore the Python summariser, but inline it into `server.py` rather than calling it as a subprocess. No subprocess pool, no JSON-over-pipes IPC, no SummarisePool — a function call inside `enrich_results()`. Snippets in the response come from the inline summariser; Zettair's `summary` field in the JSON output is ignored.

---

## Design

### Source text

The summariser reads document text from a new disk-resident store: `enwiki_top1m.docstore` + `enwiki_top1m.docmap`. Same FlatStore pattern as snippets, images, urls — flat binary file plus offset map JSON. The map (~50 MB) loads at startup. The store stays on disk and is read with `os.pread()` per result.

`build_docstore.py` already produces these files and is already invoked by `setup.sh`. Right now nothing reads them — they're stale leftovers from before the C summariser. PRD-016 puts them back into use.

The docstore content is the cleaned plain text from each article — `build_docstore.py` strips wiki markup and citation patterns at build time, so the summariser sees prose, not noise. This was a major reason the Python version produced clean output.

### Algorithm

Same as the deleted `summarise.py`. The full implementation lives at commit `0cab5de`. Re-add as `summarise.py` in the `zettair-search` repo:

- `split_fragments(text)` → list of strings
- `_is_prose(fragment)` → bool, reject headings/captions/citations
- `score_fragment(fragment, query_terms)` → float (hits / length)
- `summarise_doc(text, query_terms)` → snippet string with top-3 fragments

No design changes. The algorithm worked.

### Integration

`server.py` changes:

```python
import summarise   # new module
...

DOCSTORE_PATH = os.environ.get("ZET_DOCSTORE", "...")
DOCSTORE_MAP_PATH = os.environ.get("ZET_DOCMAP", "...")
_docstore = FlatStore(DOCSTORE_PATH, DOCSTORE_MAP_PATH, "docstore")
...

# In lifespan startup:
_docstore.load()

# In enrich_results():
def enrich_results(results: list, query: str) -> list:
    query_terms = summarise.parse_query(query)  # lowercase, drop stopwords, ...
    enriched = []
    for r in results:
        docno = r.get("docno", "")
        text = _docstore.get(docno) or ""
        snippet = summarise.summarise_doc(text, query_terms) if text else ""
        # fall back to pre-baked snippets if docstore lookup failed
        if not snippet:
            snippet = _snippets_store.get(docno) or ""
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

The `query` argument needs to flow into `enrich_results()` from the `/search` endpoint — currently it doesn't, so the call site changes too.

### Zettair changes

None. Zet keeps emitting the `summary` field — we just stop reading it. Eventually we could drop `--summary=plain` from the worker args to save a few cycles per query, but that's optional.

### Performance

Estimate per query (10 results):
- 10× `os.pread()` on docstore: ~1ms total
- 10× `summarise_doc()` calls: ~5–20ms total (depends on article length and Python regex speed)
- One-time `parse_query()`: <1ms

So total summarisation overhead per query: ~5–20ms. Current p50 latency is ~250ms; p95 ~500ms. This adds to the existing query latency but stays inside the same order of magnitude. Acceptable.

If summarisation turns out to be the new bottleneck, options:
- Run summarisation for the 10 results in parallel via `ThreadPoolExecutor` — Python releases the GIL inside `re.split()` for long inputs.
- Compile the hot loop with `mypyc` or Cython.
- Port the algorithm to C inside Zettair (PRD-017 territory).

These are all future work. PRD-016 is just "make snippets readable again, inline, no subprocess".

### What gets removed

Once the inline summariser is live and verified:
- The C summariser's output (`r.get("summary")`) is no longer consulted in `enrich_results()`. The field is still emitted by zet — harmless, just unused.
- Eventually: drop `--summary=plain` from the zet worker args in `ZetPool.start()`. This saves Zettair the work of generating summaries we don't use. Small perf win, ~1ms per query.

PRD-011 (the C summariser) becomes effectively superseded. Its remaining contribution is the `summary` field in zet's JSON output, which we no longer use. Worth marking PRD-011 as superseded by PRD-016 in the doc index.

---

## Memory cost

| Component | Size |
|---|---|
| Docstore offset map (`enwiki_top1m.docmap`) | ~50 MB |
| Docstore file content | on disk, not in RAM |
| `summarise.py` constants (stopwords, prose verbs, regex) | <1 MB |

Net: server RAM goes from ~700 MB to ~750 MB. Comfortable.

---

## API changes

`/search` response: the `snippet` field will look different. More prose, fewer captions. No structural change. Frontend doesn't change.

---

## Files Changed

| File | Change |
|---|---|
| `summarise.py` | Restored from commit `0cab5de` |
| `server.py` | Add `_docstore` FlatStore, import summariser, use in `enrich_results()`, thread query through call site |
| `deploy/zettair-search.service` | No change (`ZET_DOCSTORE`/`ZET_DOCMAP` env vars are already set) |
| `deploy/setup.sh` | No change (docstore is already built) |
| `prd/PRD-011-c-summariser.md` | Marked superseded by PRD-016 |

---

## Implementation Order

1. Restore `summarise.py` from git history (`git show 0cab5de^:summarise.py > summarise.py`).
2. Modify `server.py`: add `_docstore` FlatStore, load at startup, close on shutdown.
3. Modify `enrich_results()` signature to take the query string.
4. Update the `/search` handler to pass the query through.
5. Replace the snippet logic in `enrich_results()` to call `summarise.summarise_doc()`, falling back to the pre-baked store on missing docstore entries.
6. Local test: run the server against a small index, query, verify snippets are sensible.
7. Push, deploy, restart on server.
8. Spot-check 10 representative queries on the live site.
9. Mark PRD-011 superseded.

No data migration. No reindex. No service downtime beyond a single restart.

---

## Success Criteria

1. Snippets in `/search` responses are paragraphs from the article body, not captions or single-line headings.
2. For queries with stopwords like "the beatles" or "the rolling stones", the snippet contains the proper noun in context, not just sentences full of "the".
3. Mean query latency increase ≤ 50ms (target: ~20ms) on the 1.5M corpus.
4. No regression in result count, ranking, or response shape.
5. Server memory increase under 100 MB at startup.
