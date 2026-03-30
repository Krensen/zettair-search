# PRD-008: Query-Biased Summaries

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-03-30

---

## Problem

Current snippets are pre-baked during the Wikipedia XML parse (`wiki2trec.py`). Every query that returns the "London" article shows the same first-paragraph extract, regardless of whether the user asked for "London history", "London weather", or "London Eye height". This is a significant quality gap — the snippet often doesn't visually confirm relevance to the user's actual query.

---

## Goal

Replace static pre-extracted snippets with query-biased summaries generated at query time, using the original code from the paper:

> **"Efficient Generation of Query-Biased Summaries"**  
> Turpin, Hawking & Williams, SIGIR 2003

The code lives at `summarise/raw/` in this workspace. We use the **raw** implementation (operates on pre-parsed plain text), not the `ints/` variant (operates on vbyte-encoded word integer streams). The `ints/` approach is faster but requires building a word vocabulary and re-encoding the entire corpus — unnecessary complexity at our corpus size.

---

## How the Summariser Works

The summariser scores text passages by query term density and returns the highest-scoring window of text. Two binaries are involved:

### `parse` — builds the document store
Reads a TREC-format document collection and produces:
- `<name>.parsed.doc` — plain text of all documents, concatenated, HTML stripped
- `<name>.raw.map` — vbyte-encoded byte offsets (one per doc) for direct seeks
- `<name>.docnos` — TREC docno strings in insertion order (for hash table lookup at query time)

### `summarise` — generates summaries at query time
Reads a query file (one query per line, format below), seeks directly to each document via the map, scores passages, and outputs the best window.

**Query file format** (one line per query):
```
<num_terms> <term1> <term2> ... <num_docs> <docno1> <docno2> ...
```

Example:
```
2 black hole 3 WP_Black_hole WP_Hawking_radiation WP_Neutron_star
```

The summariser loads the `.docnos` hash table at startup and converts TREC docno strings to integer document numbers (which are byte-offset indices into the map). Documents are sorted by docno (= disk offset) before seeking, minimising disk seeks.

---

## Architecture

### New files to build

#### `build_docstore.py`
Python script. Reads `simplewiki.trec`, strips MediaWiki/HTML markup, and writes:
- `wikipedia/simplewiki.parsed.doc` — plain text corpus (~1.3GB estimated)
- `wikipedia/simplewiki.raw.map` — vbyte-encoded byte offsets
- `wikipedia/simplewiki.docnos` — one TREC docno per line (e.g. `WP_London`)

This replaces the pre-baked snippet extraction with full article text storage. The existing `simplewiki_snippets.json` remains as a fallback.

#### `summarise/raw/` — compile as-is
The raw summariser compiles cleanly with `gcc`. We'll build it on first deploy and place the binary at `summarise/raw/summarise`.

Minor fixes needed:
- `main()` return type (implicit `int` — harmless warning, worth fixing)
- `docfp` is opened but `vfp` (the global used by `readdoc.c`) is never set in `parse.c` — this is a bug in the original; `vfp` must be assigned after `fopen()`

### Changes to `server.py`

Add a `SummarisePool` class (mirrors `ZetPool`):
- Wraps the `summarise` binary as a persistent subprocess
- Accepts: query terms + list of TREC docnos
- Returns: dict of `{docno: snippet_text}`
- Falls back to `simplewiki_snippets.json` on error or timeout

**Query flow (per search request):**
1. ZetPool returns top-N results with TREC docnos
2. Extract query terms (same stemming/lowercasing as Zettair)
3. Build summariser query line: `{n_terms} {term1} ... {n_docs} {docno1} ...`
4. Write to summariser stdin, read response
5. Inject generated snippets into result JSON

**Subprocess protocol** (new — the existing `summarise` binary reads from a query *file*, not stdin):
The original code calls `fgets()` from a `FILE *queryfp` — we'll modify it minimally to read from stdin when no `-q` flag is given. One line change.

### Fallback behaviour
- If `SummarisePool` is not running or returns an error: use `simplewiki_snippets.json` as before
- If a specific docno has no entry in the doc store: use the pre-baked snippet
- Configurable via env var `ZET_SUMMARISE=1` (default off until validated)

---

## Query Term Handling

Zettair lowercases and stems query terms internally. The summariser scores by term presence in text windows. We should pass:
- The original user query terms (lowercased, stop words removed)
- Not Zettair's internal stemmed forms — the summariser does its own text scanning

The non-word list at `summarise/utils/wt10g/nonwords.top127` gives us stop words to strip.

---

## Output Format

The summariser currently `printf()`s raw text. We'll modify it to output one JSON line per document:
```json
{"docno": "WP_London", "summary": "London is the capital city of England..."}
```

This makes parsing robust and consistent with the JSONL pattern used elsewhere in the system.

---

## Performance

The summariser seeks directly to each document via the map — it does not scan the entire corpus. For 10 results, it performs 10 `fseek()` + `fread()` calls on a ~1.3GB file. On the iMac's SSD, this should be well under 10ms per query.

The `SummarisePool` keeps the binary resident (no fork/exec overhead per query), same pattern as `ZetPool`. Pool size: 1 worker (summaries are fast, no parallelism needed).

---

## Implementation Phases

### Phase 1 — Build document store
- Write and run `build_docstore.py`
- Verify: correct doc count (256,523), spot-check a few articles for clean text
- Verify map correctness: seek to doc N, confirm text matches known article

### Phase 2 — Fix and compile the summariser
- Fix `vfp` assignment bug in `parse.c`
- Add stdin support (remove mandatory `-q` flag)
- Add JSONL output mode
- Compile: `gcc -O2 -I../common -o summarise summarise.c ../common/*.c`
- Smoke test with a known query against the wt10g test data in `utils/wt10g/`

### Phase 3 — Wire into server.py
- Implement `SummarisePool`
- Add `ZET_SUMMARISE` env var gate
- Update `/search` endpoint to call summariser and inject results
- Fallback to pre-baked snippets on error

### Phase 4 — Validate and tune
- Manual spot checks: search "black hole event horizon" — does snippet show relevant sentence?
- Compare against pre-baked snippets for 20 queries
- Tune summary window length (currently fixed in `summarise.c` — expose as a constant)

---

## Files Changed

| File | Change |
|------|--------|
| `summarise/raw/parse.c` | Fix `vfp` assignment bug |
| `summarise/raw/summarise.c` | Add stdin mode; add JSONL output |
| `wikipedia/build_docstore.py` | New — builds `.parsed.doc`, `.raw.map`, `.docnos` |
| `server.py` | Add `SummarisePool`; update `/search` to inject query-biased snippets |
| `README.md` | Add Phase 4 — doc store build + summariser setup |
| `.gitignore` | Add `wikipedia/simplewiki.parsed.doc` (too large for git) |

---

## Out of Scope

- The `ints/` (integer-encoded) implementation — deferred until full English Wikipedia migration
- Snippet caching / persistence — not needed at this corpus size and query volume
- Highlighted query terms in snippet HTML — nice to have, Phase 5

---

## Success Criteria

1. Searching "black hole event horizon" returns a snippet containing "event horizon" — not a generic first-paragraph summary
2. Searching "London Eye height" returns a snippet about the Eye, not "London is the capital of England"
3. Query latency increase ≤ 20ms (p95) compared to pre-baked snippet lookup
4. Zero errors in existing load test (100 QPS) with `ZET_SUMMARISE=1`
5. Graceful fallback: if summariser crashes, pre-baked snippets serve instead
