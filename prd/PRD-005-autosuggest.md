# PRD-005: Autosuggest

**Status:** Approved  
**Priority:** High  
**Complexity:** Medium  

---

## Problem

The search box has no suggestions. Users must type and submit a full query before seeing results. This is slower and less discoverable than a modern search experience.

---

## Goal

As the user types, show a ranked dropdown of matching Wikipedia article titles drawn from real search traffic data, so users can find what they want faster with fewer keystrokes.

---

## Background Research

### What Google does
- Fires suggestions after **1 character** (observed empirically, March 2026)
- Shows **8–10 suggestions** in a dropdown
- Mixes personalised history, trending queries, and entity knowledge (photos, subtitles)
- Uses prefix matching as the primary mechanism

### Our approach
We have no personalisation or trending data yet. We use **Wikipedia Clickstream** (Wikimedia's monthly dump of search-engine-referral → article pairs) as a popularity signal.

From January 2025 English Wikipedia clickstream:
- 4M `other-search` rows (search-engine arrivals)
- 158k of those articles exist in our Simple English index
- 152k unique "fake queries" (article titles → lowercase, underscores → spaces, disambiguation stripped)
- Top query: "donald trump" — 3.2M clicks in one month

These are navigational queries — people who already knew the entity they wanted and searched for it by name. That's exactly the autosuggest use case.

---

## Data Pipeline (offline, run once)

1. **Input:** `clickstream-enwiki-2025-01.tsv.gz` + `simplewiki_titles.txt`
2. **Filter:** `other-search` rows only, article must exist in simplewiki index
3. **Transform:** `Article_Title_(disambiguation)` → `"article title"` (lowercase, strip parens)
4. **Skip:** Lists, indices, outlines, articles starting with years/numbers, titles > 6 words
5. **Output:** `autosuggest.json` — array of `[query, click_count]` pairs, sorted **alphabetically** (for binary search at query time)

Format:
```json
[
  ["aaron rodgers", 312000],
  ["abba", 187000],
  ["abraham lincoln", 445000],
  ...
]
```

Size estimate: ~152k entries × ~30 bytes = ~5MB in memory.

---

## Server Changes

### Startup
Load `autosuggest.json` into memory as a sorted list of `(query, count)` tuples.

### New endpoint
```
GET /suggest?q=don&n=8
```

**Algorithm:**
1. Lowercase the query prefix
2. Binary search the sorted list for the first entry starting with the prefix
3. Scan forward linearly collecting all matches (stop when prefix no longer matches)
4. Sort candidates by `count` descending
5. Return top `n`

**Response:**
```json
{
  "q": "don",
  "suggestions": [
    {"query": "donald trump", "count": 3238995},
    {"query": "don mclean", "count": 42000},
    {"query": "donna summer", "count": 38000}
  ]
}
```

**Performance:** < 1ms per request (binary search + linear scan on 152k sorted entries).

---

## Frontend Changes

### Trigger
- Fire after **2 characters** (not 1 like Google — our corpus is Wikipedia-specific so single-letter results are less useful; "d" returns 8 random D-names)
- Debounce: **150ms** (faster than search, since it's local data)
- Cancel previous request if a new keystroke arrives before response

### Dropdown UI
- Appears directly below the search box, full width
- Max **8 suggestions**
- Each row: search icon (grey) + suggestion text, bold prefix match highlighted
- Hover highlight in light blue (#e8f0fe)
- Clicking a suggestion: populate search box + immediately trigger search
- **Keyboard navigation:**
  - ↑ / ↓ to move through suggestions
  - Enter to select highlighted suggestion (or submit current text if none highlighted)
  - Escape to dismiss dropdown
- Dismiss on click outside

### Styling
Match Google's autocomplete dropdown as closely as practical:
- Same width as the search box
- Rounded bottom corners (0 top radius, 8px bottom)
- 1px border matching search box border
- No shadow (keeps it clean)
- Font size: 1rem, colour: #202124

### Where it appears
- **Home page:** below the main search box
- **Results page:** below the results search box

---

## Content Policy

Blocklist the following categories (applied at data pipeline time, not at query time):
- Adult content keywords: `xxx`, `xnxx`, `onlyfans`, `rule 34`, `pornhub`
- Pure piracy sites: `1337x`, `piratebay`
- Any query containing profanity (simple word list)

These are excluded from `autosuggest.json` at build time.

---

## Acceptance Criteria

- [ ] `autosuggest.json` generated with ≥ 100k entries
- [ ] `/suggest?q=don&n=8` returns correct prefix matches ranked by count in < 5ms
- [ ] Dropdown appears after 2 characters, disappears on Escape or outside click
- [ ] Keyboard navigation works (↑↓ Enter Esc)
- [ ] Clicking a suggestion triggers a search
- [ ] Works on both home page and results page
- [ ] Prefix bold-highlighted in dropdown text
- [ ] Blocklisted terms do not appear

---

## Out of Scope

- Fuzzy/substring matching (e.g. "tower" → "Eiffel Tower") — deferred to spell correction PRD
- Personalisation (user history) — no user accounts
- Trending / recency weighting — clickstream is monthly, not real-time
- Mobile-specific UX — basic responsiveness only

---

## Files

| File | Purpose |
|------|---------|
| `wikipedia/build_autosuggest.py` | Offline pipeline: clickstream → `autosuggest.json` |
| `wikipedia/autosuggest.json` | Sorted query+count array (gitignored — generated) |
| `server.py` | Add startup load + `/suggest` endpoint |
| `index.html` | Add dropdown UI + keyboard nav |

---

## Rollback

`git checkout checkpoint-1b` restores the pre-autosuggest state. `autosuggest.json` is gitignored so no data cleanup needed.
