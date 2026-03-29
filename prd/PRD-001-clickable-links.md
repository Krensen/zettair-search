# PRD-001: Clickable Wikipedia Links

**Status:** Approved  
**Checkpoint:** Will be part of checkpoint-1

---

## Problem

Search results currently show article titles as plain text. Users can't navigate to the actual Wikipedia article from a result. This makes the engine feel like a dead end.

---

## Goal

Every search result title becomes a clickable link that opens the corresponding Simple English Wikipedia article in a new tab.

---

## Spec

### URL Construction
The DOCNO stored in our index is the article title with spaces replaced by underscores (e.g. `Black_hole`, `Albert_Einstein`). The Simple English Wikipedia URL is:

```
https://simple.wikipedia.org/wiki/{DOCNO}
```

This is deterministic — no lookup required. Pure frontend change.

### Behaviour
- Title renders as a blue underlined link (matching Google's result title style)
- Opens in a new tab (`target="_blank"`)
- Uses `rel="noopener noreferrer"` for security
- URL-encodes any characters that aren't already safe (edge case: some DOCNOs may contain parentheses, commas)

### Visual
- Matches existing Google-style title colour (`#1a0dab`)
- Hover: underline appears
- Visited: colour changes to purple (`#609`) — standard browser behaviour, no custom CSS needed

---

## Out of Scope
- We are not fetching or verifying that the Wikipedia page exists
- We are not handling redirects (e.g. if Wikipedia redirects the article)
- We are not linking to full English Wikipedia (Simple English only for now, matches our index)

---

## Done When
- [ ] All result titles are clickable links
- [ ] Click opens correct Simple English Wikipedia URL in new tab
- [ ] Titles with special characters (parentheses, commas, apostrophes) resolve correctly
- [ ] No visual regression on desktop or mobile
- [ ] Spot-check: "Black hole", "Albert Einstein", "New Zealand", "C (programming language)" all link correctly
