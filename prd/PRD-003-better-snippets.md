# PRD-003: Better Snippets

**Status:** Approved  
**Checkpoint:** Will be part of checkpoint-1

---

## Problem

Current snippets come from Zettair's built-in `--summary=plain` output. This grabs early text from the document but often includes wikitext artifacts, incomplete sentences, and reference lists. The quality is poor.

---

## Goal

Each result shows a clean, human-readable 2–3 sentence summary extracted at index time and stored in a sidecar file. Built entirely by us, no external API.

---

## Spec

### At Index Time (wiki2trec.py changes)
During XML conversion, after cleaning the article text, extract the first 2–3 complete sentences to use as the snippet:

- Take the cleaned article text (after wikitext stripping)
- Find sentence boundaries using a simple heuristic: split on `. ` followed by a capital letter
- Take enough sentences to reach **300–500 characters**, stopping at a sentence boundary
- Never cut mid-sentence
- If the article is too short (< 100 chars), use the whole thing

Output format — `snippets.json` alongside the TREC file:
```json
{
  "Black_hole": "A black hole is a place in space where gravity pulls so much that even light cannot get out. The gravity is so strong because matter has been squeezed into a tiny space.",
  "Albert_Einstein": "Albert Einstein was a German-born physicist. He is best known for developing the theory of relativity.",
  ...
}
```

### At Query Time (server.py changes)
- Load `snippets.json` once at startup into memory (it'll be ~50MB, fine)
- For each result, look up snippet by DOCNO
- If found: return it as `snippet` in the JSON response
- If not found: fall back to Zettair's own snippet (graceful degradation)

### Frontend (index.html changes)
- No change needed — already renders `snippet` field
- The improvement is purely in data quality

---

## Out of Scope
- We are not doing query-aware snippet highlighting (showing where the search terms appear) — that's a future feature
- We are not using NLP sentence segmentation — simple heuristic only

---

## Done When
- [ ] `snippets.json` generated during conversion (~256k entries)
- [ ] Server loads it at startup without noticeable delay
- [ ] Snippets in results are clean prose, no `[[`, `{{`, `==` artifacts
- [ ] Snippets end at sentence boundaries, not mid-word
- [ ] Graceful fallback when docno not in snippets.json
- [ ] Spot-check 20 random results — all look clean
- [ ] Memory usage after loading sidecar is acceptable (< 200MB increase)
