# PRD-022: News-Headline Fallback for Spiking Articles Without Wikipedia Events

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-13

---

## Problem

PRD-021's news-spike summaries rely on a Wikipedia article having a
recent dated event paragraph ("On 9 May 2026, …"). When that exists,
the gate extracts it, the Mac Mini summarises it, the panel shows "In
the news…". This works beautifully for events like Tristan da Cunha's
medical evacuation or Kash Patel's confirmation.

But many spiking queries have **no such paragraph**. Observed on prod:

- **Andy Burnham** spiking — clearly UK political news, but the
  Wikipedia article is biographical with no dated events recently
  added.
- **Trisha Krishnan** spiking — Indian-Tamil community attention, no
  event documented.
- **Taylor Swift** would routinely spike with no dated event prose.
- **Euphoria** spikes via the disambiguation/emotion article, which
  has no event content at all.

Today (after the PRD-021-update that lets these still appear on the
rail), they show up as plain chips with biographical knowledge panels.
That's fine but leaves a real opportunity on the table: we know
they're in the news *somewhere* (the traffic spike is hard evidence),
we just don't have content to populate the panel.

We need a gap-filler: when Wikipedia doesn't have the news, get it
from somewhere else.

---

## Goal

For spiking articles that fail PRD-021's Wikipedia paragraph gate,
fetch recent news headlines from Google News RSS, synthesise them into
an event_paragraph-equivalent, and feed the Mac Mini summariser
through the existing news-spike pipeline. Net result: a news-style
knowledge panel appears for spiking articles whose Wikipedia hasn't
been updated.

Falls back gracefully when:
- Google News returns nothing relevant.
- The fetch times out or is rate-limited.
- The synthesised paragraph doesn't have a clearly recent dated
  context.

In all those cases, the article stays on the rail as a plain chip
(current behaviour), no regression.

---

## Non-goals

- **Replacing Wikipedia paragraphs as the primary source.** When
  Wikipedia has it, we still prefer it — it's editor-curated,
  neutral, and free of headline-style sensationalism. Google News
  is the fallback, not the primary.
- **Paid news APIs.** NewsAPI / Bing News are out for now. Free
  sources only until the value is proven.
- **Per-headline citation in the rendered panel.** We don't show
  "Source: The Times" in the summary. The Mac Mini synthesises a
  neutral summary from the headlines just like it does from a
  Wikipedia paragraph. (Could add later if the panel feels
  uncited.)
- **Caching strategies beyond a simple TTL file.** No Redis, no
  CDN. Filesystem cache on the same volume as everything else.

---

## High-level design

```
  fetch_trending.py
       │
       ▼
  apply_specificity_gate(items)
       │
       ├──▶ item has event_paragraph?  ── yes ──▶ keep as today (Wikipedia-sourced)
       │
       └──▶ no event_paragraph
              │
              ▼
       NEW: fetch_news_headlines(query)
              │
              ├──▶ headlines found, recent  ──▶ synthesise event_paragraph,
              │                                  populate event_date with the
              │                                  most-recent headline date,
              │                                  set event_source = "news_rss"
              │
              └──▶ nothing usable           ──▶ keep item, no event fields
                                                  (plain chip, biographical panel)
```

Everything downstream stays the same: news-summary producer enqueues
the job, Mac Mini uses build_news_prompt, installer drains, server
serves on spike+grace-window.

---

## Pieces

### 1. New fetcher: `fetch_news_headlines(query)`

Lives in `tools/fetch_trending.py` alongside the existing Wikipedia
fetch.

```python
def fetch_news_headlines(query: str) -> list[dict] | None:
    """Fetch the top news headlines for a query via Google News RSS.

    Returns a list of {title, link, pub_date, source} dicts, or None
    on any failure (timeout, parse error, empty result). The caller
    treats None and [] interchangeably as "no usable signal"."""
```

URL form:
```
https://news.google.com/rss/search?q={urlencode(query)}+wikipedia&hl=en-US&gl=US
```

The `+wikipedia` qualifier biases results toward news about the
Wikipedia subject rather than generic search noise. Empirically
works well for celebrities and politicians; less critical for events.

Parsing: standard `xml.etree.ElementTree` on the RSS response. Each
`<item>` has `<title>`, `<link>`, `<pubDate>`, `<source>`. We keep up
to N=5, filter out anything older than `NEWS_FRESHNESS_DAYS = 14`,
and return the rest.

Timeout: 8 seconds. On any error (network, XML parse, no items)
return None and let the caller fall through.

User-Agent: matches existing PRD-020 pattern
`zettair-search/PRD-022 (zettair.io; hugh@viaaltoadvisors.com)`.

### 2. Cache

Google News rate-limits aggressively. Cache responses per-query on
disk:

```
/mnt/wikipedia-source/trending/news_cache/<query_norm>.json
  {
    "fetched_at": "2026-05-13T02:00:00Z",
    "headlines": [...]
  }
```

TTL `NEWS_CACHE_HOURS = 6`. Within that window we re-use the cached
result rather than refetching. Two reasons:

1. We poll every 3 hours; news doesn't change *that* fast.
2. Hammering Google News from one IP every 3 hours for 50+ queries
   is exactly how we'd get blocked.

Pruning: any cache file older than 7 days is deleted on the next
fetch cycle. No separate compaction job needed.

### 3. Synthesised event_paragraph

For an article that fails Wikipedia gate but gets news headlines, we
build a synthesised paragraph:

```
Recent news about Andy Burnham:
- "Burnham steps up criticism of Starmer leadership" (The Times, 12 May 2026)
- "Mayor's allies rally support amid leadership speculation" (Guardian, 11 May 2026)
- "Burnham denies plotting Labour challenge" (BBC News, 11 May 2026)
```

That string is shoved into `event_paragraph` exactly like a Wikipedia
paragraph would be. The Mac Mini's existing news prompt (which says
"summarise the recent event from the source paragraph below") handles
it correctly — headlines + dates + sources is enough context for the
model to write a 60-100 word "what's happening now" paragraph.

We also set:

- `event_date` = max(parsed pub_date) across the headlines (so the
  serve-time STALE_NEWS_DAYS_SERVE check still works).
- `event_source` = `"news_rss"` (new field, present only for
  fallback items — used by analytics + optionally rendered as "via
  news" in the UI). Wikipedia-sourced items get
  `event_source = "wikipedia"`.

### 4. Integration into `apply_specificity_gate`

The gate currently enriches with Wikipedia paragraphs when found.
Extended:

```python
def apply_specificity_gate(items):
    for item in items[:MAX_CANDIDATES_TO_GATE]:
        # Wikipedia path (existing)
        wt = fetch_article_wikitext(item.docno)
        ev = find_event_paragraph(wt, today) if wt else None
        if ev:
            item.event_paragraph = ev.paragraph
            item.event_date = ev.event_date
            item.event_source = "wikipedia"
            kept.append(item); continue

        # News-headline fallback (new)
        headlines = fetch_news_headlines_cached(item.query)
        if headlines:
            item.event_paragraph = synthesise_paragraph(headlines, item.title)
            item.event_date = max(h.pub_date for h in headlines)
            item.event_source = "news_rss"
            kept.append(item); continue

        # Neither worked — still keep on rail (no panel)
        kept.append(item)
```

The Wikipedia call has priority because it's free and curated. We
only spend Google News fetches on the gap.

### 5. Mac Mini and server: no changes

The news-summary producer reads `event_paragraph` from
`current.json`. It doesn't care whether the paragraph came from
Wikipedia or RSS. Same prompt, same output shape. Same server-side
serve logic.

This is the design's main virtue: zero new plumbing on the
generation side.

### 6. Configuration

```python
NEWS_RSS_URL_TEMPLATE = (
    "https://news.google.com/rss/search?"
    "q={query_plus_wikipedia}&hl=en-US&gl=US"
)
NEWS_FETCH_TIMEOUT_S = 8
NEWS_CACHE_HOURS = 6
NEWS_FRESHNESS_DAYS = 14   # discard headlines older than this
NEWS_MAX_HEADLINES = 5
NEWS_CACHE_DIR = TRENDING_DIR / "news_cache"
```

All overridable via env vars for tuning.

---

## Milestones

### M1 — Fetcher + cache (~half-day)

- `fetch_news_headlines(query)` with XML parsing, freshness filter,
  timeout, graceful failure.
- `fetch_news_headlines_cached(query)` wrapping the above with TTL
  cache on disk.
- Cache pruning on each call (skip recently-touched files cheaply;
  rm anything older than 7 days).
- Unit test against a captured RSS response so we're not actually
  hammering Google in CI.

### M2 — Synthesis + gate integration (~half-day)

- `synthesise_paragraph(headlines, title)` builds the string above.
- `apply_specificity_gate` extended with the fallback path.
- `event_source` field added to current.json items.
- Backwards-compatible: items without `event_source` are assumed
  to be Wikipedia-sourced.

### M3 — Observe + tune (~half-day)

- Log `with_para_wikipedia=N` and `with_para_rss=N` separately in
  the gate output.
- Watch for a week. Tune NEWS_FRESHNESS_DAYS, +wikipedia qualifier,
  or NEWS_MAX_HEADLINES based on what we see.

### M4 (deferred) — Per-headline citation in the UI

If users want to see where the news came from, add a "Sources: The
Times, Guardian, BBC" footer below the panel. Requires plumbing
sources through the Mac Mini output (currently the summary is
plain prose, no citations). Out of scope for now.

---

## Risks

- **Google News rate-limits us.** Most likely outcome if we get
  greedy. Mitigations: 6-hour cache, top-50 cap on candidates, only
  fall back on Wikipedia-gate misses. If we still get blocked, the
  fetcher returns None and the chip stays plain — no user-visible
  breakage.

- **Hallucination.** News headlines are written to be punchy and
  sometimes misleading. The Mac Mini might synthesise a summary
  that's confidently wrong about an entity (e.g. confusing two
  people with similar names in the headlines). Mitigation: news
  prompt explicitly says "use only facts in the source", but
  small models still wander. Worst case: a misleading panel.
  Acceptable risk for the gap-fill use case; we can iterate the
  prompt if it goes badly.

- **News headlines for ambiguous names.** "Trisha Krishnan" might
  return news about other Trishas. The `+wikipedia` qualifier
  helps but isn't perfect. Mitigation: if no headlines pass the
  freshness filter or the title doesn't appear in any headline
  text, skip — drop back to plain chip.

- **Spammy / SEO / clickbait sources** dominate Google News for
  some queries. The synthesised paragraph could look slimy. Hard
  to prevent without a source-allowlist; might revisit if we see
  this in practice.

- **TOS-grey.** Google's TOS for News RSS is ambiguous. At low
  volume (≤50 queries × 4 cycles/day = 200 requests/day) we're
  well below "abuse" territory and they tolerate this. Worth
  noting we're not committing infrastructure to it; if Google
  blocks us we lose a feature, not the site.

---

## Open questions

- **Should the panel say "via news" when sourced from RSS?**
  Argument for: transparency, user knows it's not Wikipedia-vetted.
  Argument against: visual noise, breaks the consistency of the
  "In the news" panel. Default to no for v1; revisit if any reader
  complains about a wrong panel.

- **Do we cache successful results separately from failures?** A
  successful fetch yielding zero recent headlines is different
  from a timeout. Currently we'd cache None for either. Probably
  fine; both mean "don't bother re-fetching for 6h". Worst case:
  a transient timeout means a 6h delay before we retry, by which
  point the article might no longer be spiking.

- **Should we promote the news-RSS-sourced paragraph to "biography"
  when the article *also* has biographical content?** No — keep
  them separate. Wikipedia biographical summaries are stable,
  news is transient. The current spike-set logic correctly
  serves news during the spike window and falls back to bio after.

- **Other free news sources.** GDELT, Wikipedia featured-feed JSON,
  Mastodon trends. Not in scope for this PRD — Google News RSS
  alone is the v1 source. Each could be a future fallback layer
  if Google blocks us or coverage gaps emerge.
