# PRD-011: Use Zettair's Built-in C Summariser

**Status:** Superseded by PRD-016  
**Priority:** Medium  
**Complexity:** Low (C patch + server.py simplification)

---

## Problem

PRD-008 implemented query-biased summaries via a Python subprocess (`summarise.py`) that reads from a separate docstore (`enwiki.docstore` + `enwiki.docmap`). This works, but it duplicates functionality that already exists inside Zettair itself.

Zettair has a full query-biased summariser built into `devel/src/summarise.c` (~950 lines, Turpin/Hawking/Williams SIGIR 2003). It operates directly on the indexed text at query time. The problem is that when `--output=json` is active, the summary field is silently dropped — `commandline.c` only emits `rank`, `docno`, `score`, and `docid` in JSON mode. Summaries are only printed in plain-text mode. The Python summariser was built to work around this gap.

---

## Goal

Wire the C summariser's output into the JSON Lines protocol, then remove the Python summariser, docstore, and all associated infrastructure.

---

## Why This Is Better

- **No separate docstore.** The C summariser reads directly from the Zettair index — no `enwiki.docstore` (~500MB) or `enwiki.docmap` (~500MB RAM) needed.
- **No Python subprocess pool.** `SummarisePool`, `summarise.py`, and the `ZET_SUMMARISE` env var gate all go away.
- **Correct implementation.** `summarise.c` is the actual paper implementation. `summarise.py` is a reimplementation that approximates it.
- **Faster.** The C code operates on vbyte-encoded integer streams already in the index — no disk seek to a separate file per query.
- **Less moving parts.** Fewer files, fewer processes, fewer failure modes.

---

## The Fix

### 1. `commandline.c` — add summary to JSON output

In `print_results()`, the JSON branch (around line 1267) currently emits:

```c
fprintf(stdout, "{\"rank\":%u,\"docno\":\"%s\",\"score\":%.2f,\"docid\":%lu}\n",
      start + i + 1, escbuf, result[i].score, result[i].docno);
```

Change to include the summary field:

```c
char sumbuf[4096];
json_escape(result[i].summary, sumbuf, sizeof(sumbuf));
fprintf(stdout, "{\"rank\":%u,\"docno\":\"%s\",\"score\":%.2f,\"docid\":%lu,\"summary\":\"%s\"}\n",
      start + i + 1, escbuf, result[i].score, result[i].docno, sumbuf);
```

`json_escape()` is already defined in the same file (line 1231). `result[i].summary` is already populated by the existing summariser when `--summary=plain` is passed — which it already is in `server.py`'s zet command args. This is a two-line change.

### 2. `server.py` — remove Python summariser infrastructure

Remove:
- `ZET_SUMMARISE` env var and all conditional blocks gated on it
- `SummarisePool` class and `_summ_pool` instance
- `_docstore` (`FlatStore` for docstore/docmap) and its load call
- `DOCSTORE_PATH`, `DOCMAP_PATH` config vars
- The `get_many()` call and `qb_snippets` dict in `/search`

Change `enrich_results()` to read the summary directly from the zet result:

```python
snippet = r.get("summary") or _snippets_store.get(r.get("docno", "")) or ""
```

The pre-baked snippet store remains as a fallback for results where zet returns an empty summary (very short articles, encoding edge cases).

### 3. Service file — remove docstore paths, remove `ZET_SUMMARISE`

Remove from `zettair-search.service`:
```
Environment=ZET_SUMMARISE=1
Environment=ZET_DOCSTORE=...
Environment=ZET_DOCMAP=...
```

The `--summary=plain` flag is already baked into the zet command args in `server.py` — no service file change needed for that.

### 4. `build_docstore.py` — keep but make optional

`build_docstore.py` is no longer required for the summariser, but the docstore is also used as a fallback in the current architecture. Once the C summariser is validated and the fallback is confirmed unnecessary in practice, `build_docstore.py` and the `enwiki.docstore`/`.docmap` files can be removed entirely. For now, skip calling it from `setup.sh`.

---

## Files Changed

| File | Change |
|------|--------|
| `zettair/devel/src/commandline.c` | Add `summary` field to JSON Lines output |
| `zettair-search/server.py` | Remove `SummarisePool`, docstore, `ZET_SUMMARISE` gate; read summary from zet result |
| `zettair-search/deploy/zettair-search.service` | Remove `ZET_SUMMARISE`, `ZET_DOCSTORE`, `ZET_DOCMAP` env vars |
| `zettair-search/deploy/setup.sh` | Remove `build_docstore.py` call |

---

## What Is Not Changed

- `summarise.c` itself — no changes needed, it already runs correctly
- `--summary=plain` flag in `server.py`'s zet args — already there
- Pre-baked snippet store (`enwiki_snippets.store`/`.map`) — kept as fallback
- `summarise.py` — deleted

---

## Disk and RAM Savings

| Resource | Current | After |
|---|---|---|
| `enwiki.docstore` | ~500MB disk | gone |
| `enwiki.docmap` (RAM) | ~500MB at startup | gone |
| Python summariser subprocess | 1 persistent process | gone |
| `summarise.py` | 216 lines | gone |

---

## Test Plan

**T1 — Summary appears in JSON output**
- Run `echo "black hole" | ./zet -f index --okapi --summary=plain --output=json -n 5`
- Verify each result line contains a non-empty `"summary"` field
- Verify the summary contains query terms "black" or "hole"

**T2 — Fallback works**
- Temporarily rename `enwiki_snippets.store` to confirm server doesn't crash
- Result snippet should be empty string, not an error

**T3 — No regression in search quality**
- Run 10 known queries before and after, compare snippets
- Snippets should be at least as relevant as the Python summariser output

**T4 — Performance**
- Summary generation is already happening inside Zettair — no additional latency expected
- Confirm p95 query latency does not increase

---

## Acceptance Criteria

- [ ] JSON output from `zet` includes non-empty `summary` field for standard queries
- [ ] `server.py` no longer spawns a Python summariser subprocess
- [ ] `enwiki.docstore` and `enwiki.docmap` are no longer loaded at startup
- [ ] Search results display correct query-biased snippets in the UI
- [ ] No error in server logs on startup or under normal query load
