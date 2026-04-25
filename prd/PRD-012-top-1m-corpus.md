# PRD-012: Top-1M English Wikipedia Corpus

**Status:** Draft  
**Author:** metabot  
**Date:** 2026-04-25

---

## Problem

zettair.io currently indexes Simple English Wikipedia (~257K articles). Simple English is written for children and ESL learners — it's shallow, often incomplete, and frequently missing contemporary topics entirely. Searches for "Eiffel Tower", "Beatles", or "RAID 6" return dumbed-down articles or nothing at all. The system works technically but the demo isn't compelling.

---

## Goal

Replace Simple English Wikipedia with the **top 1,000,000 most-viewed full English Wikipedia articles**, selected by aggregate clickstream popularity over the existing 15-month window of clickstream data already on disk.

---

## Why 1M Articles

Click curve analysis against the 15-month enwiki clickstream shows:

| Coverage | Articles needed | % of all Wikipedia |
|---|---|---|
| 50% | 42,408 | 1.0% |
| 80% | 284,067 | 7.0% |
| 90% | 644,431 | 15.9% |
| 95% | 1,147,258 | 28.2% |
| 99% | 2,543,909 | 62.6% |

1M articles covers ~93% of all search-driven traffic and stress-tests the ranking pipeline at the boundary between well-trafficked and noisier articles — exactly where click prior tuning, length normalisation, and BM25 parameter choices become visible. 500K would look deceptively good because every included article is popular; 1M exposes real failure modes worth fixing.

---

## Infrastructure

The CCX13 VPS (2 vCPU, 8 GB RAM, 80 GB boot NVMe) already has an 80 GB Hetzner volume mounted at `/mnt/HC_Volume_105516320`, symlinked at `/mnt/wikipedia-source`, owned by `zettair:zettair`. All new corpus artifacts live there. The boot disk is not touched.

**Disk budget on `/mnt/wikipedia-source/` (80 GB):**

| Artifact | Size | Notes |
|---|---|---|
| `enwiki-latest-pages-articles.xml.bz2` | 23 GB | deleted after TREC build |
| Clickstream files (15 months) | 7.4 GB | already present |
| `top_titles.txt` | 50 MB | |
| `enwiki_top1m.trec` | 25 GB | estimate; see below |
| Snippets sidecar (`.store` + `.map`) | 3 GB | |
| Images sidecar (`.store` + `.map`) | 500 MB | |
| Zettair index | 8 GB | |
| Docstore + docmap | 12 GB | |
| **Peak (before bz2 deletion)** | **~79 GB** | tight; delete bz2 after TREC |
| **After bz2 deletion** | **~56 GB** | comfortable headroom |

**TREC size estimate:** Simple English produces ~1 GB TREC for 257K articles (~4 KB/article). Full English articles average ~5× longer. 1M articles × 20 KB/article = ~20 GB. Rounding up to 25 GB to be conservative. If it comes in under 20 GB the bz2 can optionally be kept; if it comes in over 25 GB the bz2 must be deleted promptly after TREC completes. The pipeline script handles this automatically (see Build Pipeline section).

**RAM budget:**
- Indexing: streaming, under 1 GB.
- `select_top_articles.py`: ~500 MB peak (4M-entry dict).
- `server.py` at query time: snippets map (`enwiki_snippets.map`) ~500 MB + images map ~50 MB + autosuggest list ~100 MB. Total: ~650 MB. Well within 8 GB.

---

## Article Length Handling

Index whole articles for v1. Do not chunk into sections.

**Rationale:** BM25 length normalisation (controlled by Zettair's `k1`/`b` parameters) handles long documents correctly — it penalises term frequency inflation in long articles, which is exactly what it's designed for. The largest full English articles (COVID-19, Donald Trump, World War II) are ~1 MB each; these will be present in the top 1M by definition but there are at most a few hundred such articles. Their effect on average document length — which is what BM25 normalisation uses — is minor at 1M corpus size.

Chunking would require a new docno scheme, changes to the click prior pipeline, and a merge step in result rendering. That complexity is not justified until there's evidence of actual quality degradation on long-document queries.

Revisit chunking if: snippets for very long articles are consistently bad (the C summariser should handle this fine), or if index size exceeds estimates significantly.

---

## Snippets Sidecar: Load into RAM

Load the snippets map (`.map` file, ~500 MB) into RAM at startup, as the existing `FlatStore` class already does. Do not switch to SQLite for this milestone.

**Rationale:** The map contains `{docno: [offset, length]}` integer pairs, not the full text. The actual snippets text lives on disk in the `.store` file; server.py seeks into it per result. 500 MB of RAM for the map is acceptable on an 8 GB machine. If RAM pressure becomes an issue under load, the monitoring signal is OOM kills — if that happens, SQLite is a one-afternoon follow-up. Don't pay that complexity cost speculatively.

---

## Title Normalisation

Clickstream titles are dbkey form: spaces replaced with underscores, first character uppercased, URL-decoded. Wikipedia XML dump `<title>` tags use display form: spaces, may include namespace prefix for non-article pages.

Matching rule: strip namespace prefix (anything before `:`) — wait, that would wrongly strip titles like "Albert:_Some_Article". Correct rule:

- **Allowlist side** (`top_titles.txt`): titles are stored as-is from clickstream (dbkey, underscores). Example: `Albert_Einstein`.
- **XML parse side** (`wiki2trec.py`): convert `<title>` to dbkey form by replacing spaces with underscores. Check namespace element (`<ns>0</ns>`) to filter to article namespace only — this is already done.
- **Match**: exact string equality after both sides are in dbkey form.
- **Edge cases**: titles with URL encoding in the clickstream (e.g. `%27` for apostrophe) are rare and will simply not match — they'll be absent from the corpus, which is acceptable. Do not implement URL decoding; it adds complexity for negligible coverage gain.

---

## Failure Handling in the Pipeline

When `wiki2trec.py` processes a title in the allowlist that doesn't appear in the dump (renamed, deleted, redirect-only, or encoding mismatch), the behaviour is:

- **Skip silently and continue.** The title is simply absent from the TREC output.
- After processing, print a summary: `{found} of {allowlist_size} allowlist titles matched in dump`.
- If fewer than 800,000 titles matched (80% of target), print a prominent warning. This indicates something is wrong with the title normalisation or allowlist, not just normal attrition.

Redirects: Zettair's `wiki2trec.py` already skips redirects (`#REDIRECT` check). Redirect pages that appear in the clickstream top 1M will simply not be indexed — the redirect target may or may not be in the top 1M independently. This is acceptable for v1.

---

## New Code

### `select_top_articles.py`

Reads all `clickstream-enwiki-*.tsv.gz` files, applies monthly decay (DECAY_RATE=0.85, same as `build_click_prior.py`), aggregates scores per article, outputs `top_titles.txt` with the top N titles in descending score order (one per line, dbkey form).

```
Usage: python3 select_top_articles.py [--top 1000000] [--out top_titles.txt]
```

The decay logic is a straight lift from `build_click_prior.py`: identify the most recent month as reference, weight each month's clicks by `0.85 ^ months_ago`. This means recently-popular articles rank higher than historically-popular-but-fading ones — the right behaviour for a search index.

Output format: plain text, one title per line, no header, no scores. wiki2trec.py loads this into a set for O(1) lookup.

### `wiki2trec.py` changes

Two additions, both backward-compatible:

**1. bz2 streaming input.** If the input path ends in `.bz2`, open with `bz2.open()` instead of `open()`. The XML parser (`ET.iterparse`) works identically on either file object. This eliminates the bunzip2-to-disk step entirely — the 23 GB bz2 is never decompressed to a separate file.

```python
import bz2
if xml_path.endswith('.bz2'):
    fh = bz2.open(xml_path, 'rb')
else:
    fh = open(xml_path, 'rb')
for event, elem in ET.iterparse(fh, events=('end',)):
    ...
```

**2. Title allowlist.** New optional `--titles <path>` argument. When supplied, load the file into a set and skip any article whose `safe_id(title)` is not in the set. When not supplied, behaviour is unchanged.

```
python3 wiki2trec.py enwiki-latest-pages-articles.xml.bz2 enwiki_top1m.trec --titles top_titles.txt
```

### `setup.sh` rewrite

The new `setup.sh` orchestrates the full pipeline end-to-end. Key changes from the current version:

- All corpus artifacts (`top_titles.txt`, TREC, index, sidecars, docstore) go under `/mnt/wikipedia-source/`
- Downloads enwiki bz2 dump to `/mnt/wikipedia-source/`
- Runs `select_top_articles.py` to produce `top_titles.txt`
- Runs `wiki2trec.py` with bz2 input and `--titles` allowlist
- **After TREC is built**: checks volume free space; if under 25 GB, deletes the bz2 automatically (with a log message)
- Runs `build_docno_map.py`, `build_click_prior.py`, `build_autosuggest.py`, `build_docstore.py` — all pointed at the new TREC file
- Builds the Zettair index in `/mnt/wikipedia-source/wikiindex/`
- Does **not** touch the existing Simple English index on the boot disk
- Each step is guarded by an existence check so partial runs can resume without restarting from scratch

---

## Build Resumability

Each pipeline step checks for its primary output before running:

| Step | Skip condition |
|---|---|
| Download bz2 | `enwiki-latest-pages-articles.xml.bz2` exists and size > 20 GB |
| `select_top_articles.py` | `top_titles.txt` exists |
| `wiki2trec.py` | `enwiki_top1m.trec` exists |
| Delete bz2 | Already deleted |
| `build_docno_map.py` | `docno_map.tsv` exists |
| `build_click_prior.py` | `click_prior.bin` exists |
| `build_autosuggest.py` | `autosuggest.json` exists |
| `build_docstore.py` | `enwiki_top1m.docstore` exists |
| `zet -i` (indexing) | `wikiindex/index.cfg` exists |

If a step fails partway (e.g. OOM during indexing), delete its output file and re-run `setup.sh`. The pipeline resumes from that step.

---

## Cutover Runbook

The new corpus is built alongside the existing Simple English files. Cutover is a single service restart with changed env vars.

**Step 1 — Verify new artifacts are complete**
```bash
ls -lh /mnt/wikipedia-source/wikiindex/index.cfg
ls -lh /mnt/wikipedia-source/enwiki_top1m.docstore
ls -lh /mnt/wikipedia-source/enwiki_top1m_snippets.store
# Confirm the index responds to queries
echo "einstein" | /opt/zettair/devel/zet -f /mnt/wikipedia-source/wikiindex/index --okapi --summary=plain --output=json -n 3
```

**Step 2 — Update systemd unit**

Edit `/etc/systemd/system/zettair-search.service`, changing the env vars:
```ini
Environment=ZET_INDEX=/mnt/wikipedia-source/wikiindex/index
Environment=ZET_CLICK_PRIOR=/mnt/wikipedia-source/click_prior.bin
Environment=ZET_SNIPPETS_STORE=/mnt/wikipedia-source/enwiki_top1m_snippets.store
Environment=ZET_SNIPPETS_MAP=/mnt/wikipedia-source/enwiki_top1m_snippets.map
Environment=ZET_IMAGES_STORE=/mnt/wikipedia-source/enwiki_top1m_images.store
Environment=ZET_IMAGES_MAP=/mnt/wikipedia-source/enwiki_top1m_images.map
Environment=ZET_AUTOSUGGEST=/mnt/wikipedia-source/autosuggest.json
Environment=ZET_DOCSTORE=/mnt/wikipedia-source/enwiki_top1m.docstore
Environment=ZET_DOCMAP=/mnt/wikipedia-source/enwiki_top1m.docmap
```

**Step 3 — Reload and restart**
```bash
systemctl daemon-reload
systemctl restart zettair-search
systemctl status zettair-search   # confirm running
curl http://localhost:8765/search?q=einstein | python3 -m json.tool
```

**Step 4 — Smoke test** (run these manually against the live service)
- "Einstein" → Albert Einstein article, full English content
- "Beatles" → The Beatles
- "Larry Mullen Jr" → returns a result (would have returned nothing on Simple English)
- "RAID 6" → returns a result
- "Eiffel Tower" → Eiffel Tower, not a stub

**Step 5 — Keep old files for one week**

The Simple English index lives on the boot disk at `/opt/zettair/wikiindex/` and `/opt/zettair/wikipedia/`. Do not delete. If anything goes wrong, reverse Step 2 and restart.

**Step 6 — Delete old files after one week**
```bash
rm -rf /opt/zettair/wikiindex/
rm -f /opt/zettair/wikipedia/simplewiki.*
rm -f /opt/zettair/wikipedia/enwiki.*   # old pre-volume artifacts if any
```

Zero downtime. The old service answers queries up to the moment of `systemctl restart`, which takes under 5 seconds to come back up.

---

## Refresh Cadence

- **Clickstream**: monthly. `refresh_clickstream.py` already handles this and is triggered manually or via cron. No changes needed.
- **Full corpus rebuild**: quarterly. The enwiki dump and the article selection both change slowly enough that a quarterly rebuild captures meaningful improvements without the operational cost of monthly multi-hour pipeline runs.
- **`top_titles.txt` refresh**: happens automatically as part of the quarterly corpus rebuild (re-run `select_top_articles.py` over the accumulated clickstream).

A cron entry for quarterly rebuild is out of scope — it will be triggered manually for now. Document the trigger command in the repo README when the build is complete.

---

## Files Changed

| File | Repo | Change |
|---|---|---|
| `wikipedia/select_top_articles.py` | zettair | New — produces `top_titles.txt` |
| `wikipedia/wiki2trec.py` | zettair | Add bz2 streaming + `--titles` allowlist |
| `deploy/setup.sh` | zettair-search | Rewrite for new pipeline |
| `deploy/zettair-search.service` | zettair-search | Updated paths (applied at cutover) |

---

## Implementation Order

1. Write and test `select_top_articles.py` locally against the clickstream files on the server — confirm it produces a sensible `top_titles.txt` (spot-check: Einstein, Beatles, COVID-19 should be near the top)
2. Add bz2 streaming to `wiki2trec.py` — test locally with a 10-article bz2 snippet
3. Add `--titles` allowlist to `wiki2trec.py` — test locally with a 100-title subset
4. Rewrite `setup.sh` — dry-run the structure without executing the long steps
5. Download enwiki bz2 dump to the volume
6. Run `select_top_articles.py` on the server
7. Run `wiki2trec.py` (expect ~4-6 hours streaming through the 23 GB bz2)
8. Delete bz2 if disk is tight
9. Run remaining pipeline steps (docno_map, click_prior, autosuggest, docstore, zet -i)
10. Verify index, then cut over

---

## Success Criteria

1. zettair.io serves results from the top-1M enwiki corpus
2. Pipeline completes on CCX13 + 80 GB volume without disk-full or OOM errors
3. "Einstein", "World War II", "photosynthesis", "Eiffel Tower", "Beatles", "Australia" return canonical full English articles
4. "Larry Mullen Jr", "Sangiovese", "RAID 6", "Rust ownership" return results (all currently return nothing)
5. `setup.sh` runs end-to-end on a clean server with no manual steps
6. No bunzip2-to-disk step anywhere in the pipeline
7. CI/CD continues to deploy on git push (unchanged)
8. Old Simple English files remain on disk for one week post-cutover
