# PRD-024: "Cite this" — One-Click Citations for Every Result

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-13

---

## Problem

Wikipedia is the single most-cited source in undergraduate papers,
secondary-school assignments, and a surprising amount of journalism.
Every student doing a research project hits this workflow:

1. Search for a topic, find the right Wikipedia article.
2. Open the article.
3. Find Wikipedia's "Cite this page" link (buried in the left sidebar
   under "Tools", hard to discover on mobile).
4. Pick a citation style from a long list.
5. Copy the formatted citation into their bibliography.

The pain point is step 3-5: discoverability and friction. Wikipedia's
own citation tool is functional but invisible and slow. Third-party
citation generators (EasyBib, Cite This For Me, Zotero) have built
real businesses around this exact friction.

We can collapse the workflow to: one click on the result card, one
click to copy a perfectly-formatted citation. No leaving the search
results page. No picking through a tools menu.

---

## Goal

Every search result and the knowledge panel get a small **"Cite"**
affordance. Clicking it opens a popover that shows the citation in
five formats — APA, MLA, Chicago, Harvard, BibTeX — each with a
one-click copy button. Closes on Escape or click-outside.

The implementation is pure frontend. No backend changes. No new
data. Uses fields already in every search response.

---

## Non-goals

- **Citation customisation** (changing author, accessed-date manually).
  Anyone needing that uses Wikipedia's own tool. We're optimising the
  90% case.
- **Authentication / saved bibliographies.** A future feature might
  pair "Cite this" with "Saved searches / reading lists" (PRD-023
  #6) into a personal bibliography, but the citation popover itself
  doesn't need accounts.
- **Direct integration with reference managers** (Zotero, Mendeley).
  Out of scope; users can paste a BibTeX string into Zotero manually.
- **Permalink to a citation popup** (`?cite=Mark_Carney`). Nice for
  sharing but not v1; data is all there for an easy follow-up.
- **Multilingual citation conventions.** English only for now.

---

## High-level design

```
result card:
  ┌──────────────────────────────────────────────────┐
  │ [favicon] Wikipedia · en.wikipedia.org › Mark_Carney
  │ Mark Carney                                   [Cite]
  │ Mark Joseph Carney is a Canadian politician…    [thumbnail]
  └──────────────────────────────────────────────────┘
                                              ▲
                                              │ click
                                              ▼
                          ┌─────────────────────────────────┐
                          │  Cite "Mark Carney"          [×]│
                          ├─────────────────────────────────┤
                          │  APA                       [Copy]│
                          │  Wikipedia contributors. (2026).│
                          │  Mark Carney. In Wikipedia.     │
                          │  Retrieved May 13, 2026, from…  │
                          ├─────────────────────────────────┤
                          │  MLA                       [Copy]│
                          │  "Mark Carney." Wikipedia, The…  │
                          ├─────────────────────────────────┤
                          │  Chicago                   [Copy]│
                          │  …                              │
                          ├─────────────────────────────────┤
                          │  Harvard                   [Copy]│
                          │  …                              │
                          ├─────────────────────────────────┤
                          │  BibTeX                    [Copy]│
                          │  @misc{wiki:MarkCarney,         │
                          │    title = {Mark Carney},…      │
                          └─────────────────────────────────┘
```

A single popover, one citation per row, copy button per row.
Sticky-positioned next to the result card; closes on Escape /
click-outside / explicit `×`.

---

## Pieces

### 1. UI affordance on each result

A small **`Cite`** link next to the result title (right-aligned, same
row as the breadcrumb / title cluster). On mobile, sits below the
title to avoid crowding. Subtle styling — text-only, blue on hover.
Don't compete visually with the result title itself.

Same affordance on the knowledge panel, positioned next to the
article title or in the source line.

### 2. Citation generator (client-side JS)

A pure function:

```js
function buildCitation(style, { title, url, accessedAt }) {
  switch (style) {
    case 'apa':     return apa(title, url, accessedAt);
    case 'mla':     return mla(title, url, accessedAt);
    case 'chicago': return chicago(title, url, accessedAt);
    case 'harvard': return harvard(title, url, accessedAt);
    case 'bibtex':  return bibtex(title, url, accessedAt);
  }
}
```

`accessedAt` is current date, formatted per style. Title comes from
the result (`formatTitle(r.docno)`). URL is `r.url` (already in the
search response).

**Author** is `"Wikipedia contributors"` for all styles — that's the
established convention and matches Wikipedia's own cite tool.

**Publication** is `Wikipedia, The Free Encyclopedia`.

**Year** is the current year (Wikipedia is a living document; styles
that need a year use the current one as Wikipedia's own tool does).

### 3. Exact formats (frozen for v1)

For Mark Carney (`https://en.wikipedia.org/wiki/Mark_Carney`),
accessed May 13, 2026:

**APA** (7th ed.):
> Wikipedia contributors. (2026, May 13). *Mark Carney*. In *Wikipedia, The Free Encyclopedia*. Retrieved May 13, 2026, from https://en.wikipedia.org/wiki/Mark_Carney

**MLA** (9th ed.):
> "Mark Carney." *Wikipedia, The Free Encyclopedia*. Wikimedia Foundation, 13 May 2026. Web. 13 May 2026.

**Chicago** (notes & bibliography):
> Wikipedia contributors. "Mark Carney." Wikipedia, The Free Encyclopedia. Last modified May 13, 2026. https://en.wikipedia.org/wiki/Mark_Carney.

**Harvard**:
> Wikipedia contributors (2026) *Mark Carney*. Available at: https://en.wikipedia.org/wiki/Mark_Carney (Accessed: 13 May 2026).

**BibTeX**:
```bibtex
@misc{wiki:MarkCarney,
  author       = "Wikipedia contributors",
  title        = "Mark Carney --- {Wikipedia}{,} The Free Encyclopedia",
  year         = "2026",
  url          = "https://en.wikipedia.org/wiki/Mark_Carney",
  note         = "[Online; accessed 13-May-2026]"
}
```

These match Wikipedia's own cite-tool conventions exactly so a user
producing citations using both tools gets consistent results.

### 4. Popover behaviour

- Opens on click of the `Cite` link.
- Positioned: by default below the link, right-aligned so it doesn't
  spill off-screen. Flips above the link if there's no room below.
- Close on: Escape key, click outside, explicit `×`, second click on
  the `Cite` link.
- Only one popover open at a time — opening another closes the first.
- Focus management: opens with focus on the first Copy button;
  Tab cycles through Copy buttons; Escape returns focus to the
  triggering `Cite` link.
- Visually: white background, subtle shadow, max-width 480px, scrolls
  if the popover overflows on small screens.

### 5. Copy interaction

Each format row has a `Copy` button. On click:

- `navigator.clipboard.writeText()` writes the formatted string.
- Button label flips to **"Copied!"** for 1.5 seconds, then back to
  **"Copy"**.
- Success state has a subtle green check colour; doesn't dismiss
  the popover (user might want to copy multiple formats).
- Fallback for browsers without `clipboard.writeText`: a hidden
  textarea + `document.execCommand('copy')` (older Safari).

### 6. Accessibility

- `Cite` link is a `<button>` (not a link, because no destination).
- Popover has `role="dialog"`, `aria-label="Cite Mark Carney"`,
  `aria-modal="false"` (we don't trap focus globally; users can
  Tab out).
- Each citation row has `aria-label="APA citation"`, with the
  citation text in a `<div>` (selectable for manual copy via
  keyboard shortcut as well).
- Copy buttons have `aria-label="Copy APA citation"`.
- Popover dismiss via Escape uses standard `keydown` listener.

### 7. Telemetry

Log an event when a citation is copied — which style, which docno.
This tells us:

- Which styles are most-used (could drop unused ones).
- Whether the feature is being adopted (success metric).
- Whether some queries get cited more than others (signal of
  research vs casual use).

Reuses the existing click-log infrastructure with a new event type.

---

## Milestones

### M1 — Core implementation (~half-day)

- Citation generator functions in `index.html` (or a small standalone
  `cite.js` if it grows).
- `Cite` link on every result card.
- Popover component with five formats and copy buttons.
- Click-outside / Escape dismissal.
- Manual smoke test on a few queries.

### M2 — Knowledge panel + polish (~2 hours)

- Same affordance on the knowledge panel.
- Mobile layout tweak (link below title when crowded).
- Subtle hover/focus styling.
- "Copied!" feedback animation.

### M3 — Telemetry (~1 hour)

- Log copy events via the existing `/click`-style endpoint (or a
  new `/event` endpoint if we want to keep clicks pure).
- Dashboard query to count cites by style / docno over the last
  week.

---

## Risks

- **Citation format drift.** Style guides update every few years
  (APA 7 → 8, MLA 9 → 10 etc). When that happens we'd want to
  update the formatter. Mitigation: keep all five formatters in
  one file, well-commented with the spec version they implement.
  Annual review.

- **Wrong-style citations.** Students using a citation in good faith
  who get marked down for a malformed entry would be embarrassing.
  Mitigation: copy the exact format used by Wikipedia's own cite
  tool (cross-checked during build). If their tool's wrong, we're
  wrong in the same way, which is at least defensible.

- **Discoverability of the `Cite` link.** A subtle text link is easy
  to miss. Mitigation: A/B-able later — could promote to a small
  icon button if usage is low after a week.

- **Mobile usability.** Popovers on mobile are awkward. Mitigation:
  on narrow screens, the popover becomes a bottom-sheet (slides up
  from bottom, full-width). Standard pattern; well-supported.

- **Clipboard permissions.** Some browsers require user-gesture for
  clipboard write; we're always in a click handler so this should
  be fine. The textarea fallback covers older browsers.

---

## Open questions

- **Include a sixth format?** IEEE is the engineering standard;
  Vancouver is medical. Both are niche but loved by their fields.
  Defer; add if requested. Five styles cover all common student use.

- **Pre-fill author field for non-Wikipedia content?** When we
  expand to other corpora (if ever), we'd need real author data.
  For now `"Wikipedia contributors"` is the only string and it's
  hard-coded. Future work would parameterise it.

- **Should BibTeX key be the docno or include the year?** Current
  design: `wiki:<docno>` (no year). Pro: stable across re-cites.
  Con: collisions if user cites the same article twice in
  different years. Defer; users can rename freely after copy.

- **Cite the news summary specifically?** When a knowledge panel
  shows a news-spike summary (PRD-021), the citation still points
  at the underlying Wikipedia article, not the synthesised summary.
  That's the right call — the article is the canonical source —
  but worth documenting so it doesn't come up as a "bug" later.
