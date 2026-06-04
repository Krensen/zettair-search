# PRD-031: Titles Sidecar — Canonical Display Titles per Docno

**Status:** Draft
**Author:** metabot
**Date:** 2026-06-05

---

## Problem

PRD-030 #7 added a `title` field to every `/search` result so the
frontend could display "Putin's Palace" instead of "Putin s Palace"
(the apostrophe-stripped docno safe_id form). The implementation was
a **hack**: it derived the title at request time by parsing the
`_urls_store` URL — `urlsplit`, `unquote`, take the slug, replace
underscores with spaces, strip the trailing `(disambiguator)`. It
works, but it is the wrong shape.

What is wrong with it:

- **Wrong source of truth.** The Wikipedia `<title>` is read by
  `wiki2trec.py` during corpus build (line 171). That canonical
  display title is then *thrown away* — used to write
  `<TITLE>Title</TITLE>` into the TREC for the per-field BM25
  scorer, used to seed the first sentence of `<TEXT>`, used to
  compute the safe_id docno — but never persisted anywhere the
  server can look it up at runtime.
- **URL parsing as title parsing.** The URL exists because dbkey ≠
  safe_id for ~23% of articles. We piggy-backed display-title
  derivation onto a sidecar built for a different purpose. The
  helper has bespoke disambiguator-stripping logic baked in
  (`re.sub(r"\s*\([^()]*\)\s*$", "", title)`) because there is no
  clean place to express "display title" vs "URL-form title".
- **Silent failure on the 77% case.** For docnos where dbkey ==
  safe_id, the URL is constructed from the docno (`f".../{docno}"`),
  so URL→title round-trips through the *lossy* docno form. The
  helper produces the same thing as the old docno-derived path for
  these — no regression, but also no win. A title with mid-word
  punctuation that *happened to round-trip cleanly* through the
  safe_id (rare but real) would still come back stripped.
- **Forks logic into conditionals about what `/wiki/` paths look
  like.** Should be one O(1) dict lookup; instead it is path
  parsing.

The fix is structural: persist the canonical title once at corpus
build time, load it into RAM at server startup, look it up directly
per result. Same pattern as every other sidecar in the system
(snippets, images, URLs, reading-time, related-entities).

---

## Goal

A new offline sidecar `enwiki_top1m_titles.{store,map}` (FlatStore
pattern, same as the others), keyed by docno → canonical Wikipedia
display title. Server loads it at startup, projects `r["title"]`
from one dict lookup per result. The URL-parsing helper deletes
entirely.

Behaviour visible to clients: identical. Frontend already prefers
`r.title` with `formatTitle(r.docno)` as fallback (shipped in PRD-030
step D); nothing changes there. iOS app (PRD-028) also keeps working
identically.

---

## Non-goals

- **Schema or API changes.** `r.title` is still a single string in
  the same place.
- **Frontend changes.** PRD-030 step D already taught both the
  knowledge panel and result cards to prefer `r.title`.
- **Removing the URL store.** `_urls_store` still serves its
  original purpose (canonical Wikipedia URL for the ~23% of dbkey-
  mismatch docnos); it just no longer has the side-job of being
  the title source.
- **Disambiguator handling on the iOS / web rendering side.** This
  PRD does not change how `(film)` etc. is rendered; whatever the
  current behaviour is (strip on web, may or may not on iOS), it is
  preserved by emitting the title with the disambiguator and
  letting the client strip if desired. See open questions.
- **A separate "short title" field for breadcrumbs.** Out of scope.

---

## Pieces

### 1. Title extraction during corpus build (zettair repo)

`zettair/wikipedia/wiki2trec.py` already has the canonical
`title = title_el.text.strip()` on line 171. Add ~6 lines:

```python
# Alongside the existing URL/snippet/image stores
title_store_path = base + '_titles.store'
title_map_path   = base + '_titles.map'
title_map  = {}
title_offset = 0

# In the inner loop, after computing docno:
encoded_title = title.encode('utf-8')
title_map[docno] = [title_offset, len(encoded_title)]
title_store.write(encoded_title)
title_offset += len(encoded_title)
```

Every docno gets a title entry — not just the dbkey-mismatch ones,
unlike the URL store. The store is small (~50 MB for 1.5M titles
averaging ~30 bytes) but covers 100% of the corpus, so the server
never has to fall back to docno-derived heuristics.

### 2. Bootstrap script for the existing index (zettair repo)

We don't want to wait for a corpus refresh to ship this. A standalone
bootstrap, mirroring `build_urls_store.py`, builds the titles sidecar
from data we already have on prod.

Two viable inputs:
- **The existing `enwiki_top1m.trec` file.** Walks the TREC, extracts
  `<TITLE>` tags, emits the sidecar. ~30 GB read, ~2 minutes wall time.
  No new dependency; the TREC is already on prod.
- **The existing `enwiki_top1m.dbkeys.tsv`.** Has `safe_id\tdbkey`;
  derive title via `dbkey.replace("_", " ")`. Loses nothing for ~95%
  of titles but does lose the disambiguator-vs-non-disambiguator
  signal where Wikipedia carries it (`Mercury_(planet)` vs `Mercury`).

Lean toward the TREC walk — full fidelity, one-shot, ~2 min one-time
cost. Same script can be re-run after a corpus refresh as a sanity
check.

### 3. Setup.sh staleness gating (zettair-search repo)

Same shape as the URL store step in `setup.sh`. Triggers a rebuild if:
- The titles sidecar files are missing, OR
- The TREC file is newer than the sidecar.

The TREC build (`wiki2trec.py`) already emits the sidecar inline if
the PR for step 1 has merged; the bootstrap path is only needed for
*existing* indexes built before this PRD. Once a full corpus refresh
runs post-merge, the bootstrap step becomes redundant. Leave it in
place anyway for resilience.

### 4. Server load + lookup (zettair-search repo)

`server.py`:

- Add `TITLES_STORE_PATH` / `TITLES_MAP_PATH` env-driven paths next
  to the other `_*_STORE` constants.
- Add `_titles_store = FlatStore(...)` in the same place as
  `_snippets_store`, etc.
- Add `_titles_store.load()` in the lifespan startup, `.close()` on
  shutdown.
- In `enrich_results()`, replace the URL-derived title with:
  ```python
  "title": _titles_store.get(docno) or _title_fallback(docno),
  ```
  where `_title_fallback` is the trivial docno-to-display transform
  (`docno.replace("_", " ")`) — used only when the sidecar is
  missing (so the server still ships *something* during deploy
  between sidecar versions).
- **Delete `_title_from_url_or_docno` entirely.**

### 5. Systemd unit env var

`deploy/zettair-search.service` gets `ZET_TITLES_STORE` and
`ZET_TITLES_MAP` env vars pointing at the sidecar files on the
volume — mirroring the existing pattern for snippets / images /
urls / etc.

### 6. README + memory

Update the data-files table in `README.md` to include the new
sidecar. Update the env vars table.

---

## Risks

- **Corpus-refresh dependency for new docnos.** Any article that
  enters the corpus between rebuilds (via the trending union path)
  won't be in the titles sidecar. Mitigation: `_title_fallback`
  handles missing keys with the existing docno-derived display, so
  no broken titles — just slightly worse ones for the long tail.
  Same gap exists today for every other sidecar.
- **Memory cost.** ~50 MB for the map + the file stays on disk.
  Negligible (the server already keeps ~13 GB of stores mmap'd).
- **The TREC walk during bootstrap is slow (~2 min) and reads 30
  GB.** Acceptable; one-time cost on a manual `setup.sh` re-run.
- **Disambiguator handling.** The Wikipedia title is `Mercury
  (planet)`. The current URL-derived helper strips the
  `(planet)` suffix. The new sidecar will store the *full* title
  including disambiguator. Frontend behaviour change: do we want to
  show "Mercury" or "Mercury (planet)"? Today web strips, iOS does
  not. Decide on the client side (probably "strip on web, keep on
  iOS knowledge panel"); not a server question.

---

## Build estimate

| Step | Time |
|---|---|
| wiki2trec.py title sidecar emit | 20 min |
| build_titles_sidecar.py bootstrap | 30 min |
| setup.sh staleness gating | 30 min |
| server.py FlatStore load + lookup | 30 min |
| zettair-search.service env var | 5 min |
| Remove _title_from_url_or_docno + breadcrumb update | 15 min |
| README + memory update | 15 min |
| Run bootstrap on prod, deploy, verify | 30 min |
| **Total** | **~3 h** |

---

## Migration plan

1. **PR to zettair repo** — wiki2trec.py inline emit + standalone
   bootstrap script. Lands first since the zettair-search PR depends
   on the sidecar existing.
2. **Run bootstrap on prod** to build the titles sidecar against
   the current TREC. Verify the file lands at the expected path.
3. **PR to zettair-search repo** — server load, setup.sh gating,
   env var in systemd, delete `_title_from_url_or_docno`, README
   update. The fallback path preserves correctness during the brief
   window where setup.sh hasn't moved the sidecar to the volume
   yet.
4. **Merge + deploy** — usual CI path.
5. **Verify** with the same Putin's Palace test from PRD-030 step
   D. Should be byte-identical to the URL-hack output but going
   through the new path.

After the URL-hack is deleted, the helper is gone, the conditional
is gone, the disambiguator regex is gone, and the title source is
one dict lookup. Done.

---

## Open questions

- **Should the titles sidecar carry the disambiguator?** Lean yes:
  the canonical Wikipedia title is `Mercury (planet)`, that's what
  we store; clients strip if they want. Mirrors how we treat other
  Wikipedia metadata.
- **Should we also strip leading/trailing whitespace on the title
  before storing?** Already does (`.strip()` on line 171 of
  wiki2trec.py). Confirmed.
- **What about non-article namespaces?** wiki2trec.py already filters
  to `ns == '0'` (article namespace). Non-articles never reach the
  TREC; non-issue.
