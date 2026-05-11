# PRD-021: News-Spike Summaries — Why-Is-It-In-The-News Knowledge Panel

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-11

---

## Problem

PRD-020's trending chip rail tells the user *what* is hot ("Tristan da Cunha,
Mark Carney, Benjamin Netanyahu") but not *why*. Clicking a chip runs a
normal search and surfaces the existing biographical knowledge-panel
summary, which is great for "who is Mark Carney" but unhelpful for "why
is Mark Carney suddenly trending today".

A user landing on a trending result expects the panel to explain the
current event: the trade war Carney is responding to, the medical evac
on Tristan da Cunha, etc. Today they get static biographical content
that doesn't change with the news cycle.

Two secondary problems we'd solve along the way:

1. **The current spike filter is purely temporal** (pageview shape) and
   admits non-newsworthy community pile-ons — Trisha Krishnan trending
   for a Tamil-language movie promo, regional politicians trending for
   election-day pile-ons. The pageview shape can't distinguish "real
   event" from "sustained community attention". We need a content
   signal too.

2. **The rail is sometimes sparse.** After tightening the noise filters
   in PRD-020 we sometimes get only 2-4 items. Users want a fuller rail
   with consistent rhythm — 6-12 chips most of the time.

---

## Goal

When a query is currently spiking, the knowledge panel shows a
news-flavoured summary describing the recent event that drove the
spike, generated offline by the Mac Mini summariser from the
Wikipedia article's own current-events content. When the spike
subsides, the panel reverts to the existing biographical summary.

The mechanism for *deciding* what's news-driven (the specificity gate)
doubles as a quality filter for the trending rail itself: only
articles where Wikipedia editors have documented a "this happened on
date X" event get on the rail. Pageview-shape-only spikes that don't
correspond to any documented event fall off.

---

## Non-goals

- **External news APIs** (Google News, NewsAPI, GDELT, Bing). The
  Wikipedia article itself is the news source — editors update the
  body within hours of significant events. Saw a clear "why is this
  trending" paragraph for Tristan da Cunha (UK military para-drop, 9
  May 2026) directly from the article text.
- **Real-time event detection.** We piggyback on Wikipedia's edit
  cadence — typically a 1-4 hour lag for big events. Articles updated
  before our next 3-hour fetch cycle get news summaries; faster ones
  miss until the cycle catches up. Acceptable.
- **Multiple news summaries per article.** One news summary per
  spiking article, refreshed periodically. Not a feed.
- **Auto-detecting that the news has changed mid-spike.** If Mark
  Carney's news changes from "trade war" to "election" mid-week, the
  refresh policy will pick it up on the next cycle. We don't try to
  detect content shift within a cycle.

---

## High-level design

```
                     pageview dumps (hourly)
                              │
                              ▼
                  fetch_trending.py
                  STEP 1: pageview-shape filter
                  → top 1000 → 3000 widened candidate pool
                  → apply existing PRD-020 filters
                  → ~15-30 candidate spikes survive
                              │
                              ▼
                  STEP 2 (NEW): article-specificity gate
                  for each candidate:
                    fetch Wikipedia article via REST API
                    strip wikitext, find paragraphs
                    score by recency-of-dated-event
                    if specificity >= threshold:
                      keep paragraph as event_paragraph
                      keep date as event_date
                    else:
                      drop the candidate
                  → 6-12 articles with a recent-event paragraph survive
                              │
                              ▼
                  current.json  (extended schema)
                  items now carry event_paragraph + event_date
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
        homepage chip rail            news-summary producer (NEW)
        (existing, unchanged)         every 3h:
                                        for each spiking item:
                                          if no news summary OR > 48h old:
                                            write pending/<query>:news.json
                                            with event_paragraph as input
                                              │
                                              ▼
                                      Mac Mini summariser
                                      (existing, with new prompt variant)
                                      → outbox/<query>:news.md
                                              │
                                              ▼
                                      installer (existing)
                                      → summaries.store under
                                        key "<query_norm>:news"
                                              │
                                              ▼
                                      server.py /search
                                      if query in current spike list AND
                                         "<query>:news" exists:
                                        return news summary
                                      else:
                                        return biographical summary
```

---

## Pieces

### 1. Widened candidate pool

`fetch_trending.py` currently keeps top-1000 articles per hourly dump.
Widen to **top-3000** so the specificity gate has a larger filter
surface. No quality loss — extra articles either pass specificity (good
catches) or don't (filtered as today).

Cost: per-sample memory grows from ~30 KB to ~90 KB. History.jsonl
grows accordingly, ~3× space, still small. ~few minutes more compaction
budget per month.

### 2. Article specificity gate

For each candidate (after pageview-shape filters), fetch the article
via Wikipedia's REST API:

```
GET https://en.wikipedia.org/w/api.php?action=parse&page=<docno>&prop=wikitext&format=json
```

with `User-Agent: ZettairSearch/1.0 (https://zettair.io; hugh@viaaltoadvisors.com)`
(the same UA pattern PRD-020 settled on for the dump fetcher).

Parse wikitext to plain paragraphs using the regex chain established
during PRD design (verified on Tristan da Cunha and Mark Carney
articles — section markup, refs, templates all strip cleanly).

For each paragraph ≥ 120 chars:

```python
date_specificity = 0
date_specificity += 4 * len(re.findall(r'\b(\d{1,2}\s+(Jan|Feb|...|Dec)\s+202[4-6])\b', p))   # day-precision
date_specificity += 4 * len(re.findall(r'\b((Jan|...|Dec)\s+\d{1,2},?\s+202[4-6])\b', p))      # US day format
date_specificity += 2 * len(re.findall(r'\b(Jan|...|Dec)\s+202[4-6]\b', p))                    # month-precision
date_specificity += 1 * len(re.findall(r'\b202[4-6]\b', p))                                    # year-only
```

A paragraph qualifies as a **recent-event paragraph** if:

- `date_specificity >= 4` (i.e. at least one day-precision date OR
  multiple month-precision dates), AND
- the most-recent dated mention in the paragraph is within
  `EVENT_FRESHNESS_DAYS = 14` of today.

If at least one paragraph qualifies, the article keeps the
highest-scoring such paragraph as `event_paragraph`, the latest dated
mention as `event_date`, and the article passes the specificity gate.
Otherwise the article is **dropped from the trending rail entirely**.

This is the load-bearing change: pageview-shape alone is not enough; we
need an editorial signal. Trisha Krishnan trending without a dated
event paragraph → off the rail. Tristan da Cunha with "On 9 May 2026,
a UK military team…" → stays on, with paragraph captured.

### 3. Wider rail target

After specificity, aim for **6-12 articles** on the rail.
`RAIL_MAX = 12`. Lower bound is aspirational — quiet news days will
yield fewer; never pad with non-spike items (that defeats the purpose).

### 4. Schema additions to `current.json`

```json
{
  "query": "mark carney",
  "title": "Mark Carney",
  "docno": "Mark_Carney",
  "rank": 1,
  "views": 14223,
  "median_baseline": 412,
  "score": 3.54,
  "event_paragraph": "During his tenure as prime minister, Carney removed the federal consumer carbon tax, enacted the One Canadian Economy Act…",
  "event_date": "2026-05-09",
  "event_specificity": 7.0
}
```

`event_paragraph` is the raw text — the summariser reads it directly,
no second Wikipedia fetch. Capped at ~2000 chars to keep
`current.json` small.

### 5. News-summary producer

New script: `tools/build_news_summary_jobs.py`. Runs on a systemd
timer (every 3 hours, offset from the trending fetch by 30 min).

For each item in `current.json` (only those with an
`event_paragraph`):

1. Check if `summaries.store` already has key
   `<query_norm>:news` written within the last 48 hours. If yes, skip.
2. Otherwise write `pending/<query_norm>:news.json` to the
   summariser queue with:

```json
{
  "schema_version": 1,
  "query": "Mark Carney",
  "query_norm": "mark carney:news",
  "mode": "news-spike",
  "event_date": "2026-05-09",
  "event_paragraph": "...",
  "results": []
}
```

The Mac Mini's existing rsync pull picks it up alongside biographical
jobs. The producer-side script is small (~80 lines).

### 6. Summariser prompt variant

`prompt.py` gets a new `build_news_prompt(job)` function:

```
SYSTEM:
You write very short knowledge-panel summaries explaining why a
Wikipedia article is currently in the news.

Output format — exactly this, nothing else:
1. ONE paragraph of 60-100 words. Not more. Hard cap.
2. Lead with the topic title in **bold**.
3. State the recent event with specific dates and names.
4. End with a brief follow-up detail if present in the source.

Strict rules:
- Use only facts in the source paragraph below. Do not invent.
- Use the past tense for the event; present tense only for ongoing
  conditions.
- No bullet points for news summaries — this is one running
  paragraph.
- Output the paragraph directly. Nothing before. Nothing after.

USER:
The article topic is "{job.query}".
The Wikipedia article was last edited around {job.event_date}. Here
is the relevant paragraph from the article body:

{job.event_paragraph}

Write the summary now.
```

Detection in `generate.py`: if `cfg["mode"] == "news-spike"` (or
equivalent), use `build_news_prompt`; else use the existing
biographical prompt.

### 7. FlatStore composite keys

Existing biographical summaries live under `query_norm` (e.g.
`mark carney`). News summaries live under `<query_norm>:news` (e.g.
`mark carney:news`). Same store, same map — composite keying needs no
new infrastructure.

`install_summaries.py` continues to read `done/<query>.md` files; the
filename carries the composite key (`mark carney:news.md`) and is
stored verbatim. The colon is safe in filenames on linux/mac.

### 8. Server-side serving

`server.py`'s `/search` handler currently looks up the biographical
summary by `query_norm`. Extend the lookup:

```python
def get_summary(query_norm):
    # If the query is currently spiking AND a news summary exists,
    # prefer the news summary. Otherwise fall through to biographical.
    if query_norm in _current_spike_queries():
        news = _summaries_store.get(f"{query_norm}:news")
        if news:
            return news, "news"
    bio = _summaries_store.get(query_norm)
    return bio, "biographical" if bio else None
```

`_current_spike_queries()` reads the same mtime-cached `current.json`
the trending endpoint reads — just projects to a set of `query_norm`s.

The response from `/search` gains a `summary_kind` field
(`"news" | "biographical" | null`) so the frontend can show a
"Trending" badge above the news-summary panel.

### 9. Frontend distinguishing news from biographical

When `summary_kind == "news"`:
- Knowledge-panel badge says "Trending now" or "What's happening"
  (different from the existing "Summary" badge).
- Subtle dated stamp: "as of {event_date}" under the panel.
- Optional: small "In the news" pill near the title.

When `summary_kind == "biographical"` (or null):
- Existing rendering, no changes.

### 10. Compaction

News summaries accumulate in the FlatStore indefinitely. Weekly
compaction job (new systemd timer):

- Iterate all `<key>:news` entries in the map.
- For each, check if the query has been on the trending rail in the
  last `STALE_NEWS_DAYS = 30` days (reading the trending
  `history.jsonl`).
- If not, delete from map. The FlatStore body keeps the bytes (orphan
  bytes are handled by PRD-018's existing rewrite-on-rebuild flow);
  the map shrink is what matters.

---

## Milestones

### M1 — Widened pool + specificity gate (~1 day)

- `fetch_trending.py`: top-3000 instead of top-1000; specificity
  scoring; per-candidate Wikipedia fetch; `event_paragraph` /
  `event_date` / `event_specificity` written into `current.json`.
- Articles failing specificity drop off the rail. Rail now aspires
  to 6-12 items.
- `/api/trending` already projects only `(query, title, in_index,
  wiki_url)`; no change there. But `event_*` fields are present in
  `current.json` for downstream consumers.

### M2 — News-summary producer + prompt (~1 day)

- `tools/build_news_summary_jobs.py` walks `current.json`,
  enqueues `<query>:news` jobs with `mode: "news-spike"` and
  `event_paragraph`.
- `prompt.build_news_prompt()` in the zettair-summariser repo.
- `generate.py` branches on `cfg["mode"]`.
- systemd timer fires every 3 hours, offset 30 min from
  `fetch_trending`.

### M3 — Server-side serving + frontend (~0.5 day)

- `server.py` reads `current.json` spike set; serves `:news` summary
  when query is spiking AND summary exists.
- Response includes `summary_kind` field.
- `index.html` renders different badge + dated stamp for news
  summaries.

### M4 — Compaction (~0.5 day)

- `tools/compact_news_summaries.py` walks `summaries.map`, drops
  `:news` entries whose subject hasn't been on the trending rail in
  `STALE_NEWS_DAYS` days.
- Weekly systemd timer.

### M5 (deferred) — Refresh policy refinement

Today's design refreshes news summaries after 48h. This is probably
right for most news but might be too slow for breaking stories (the
news changes within a day). M5 explores a content-hash refresh: if
the article's `event_paragraph` changes, force-refresh even within
48h. Defer until we see the 48h rule's actual behaviour.

### M6 (deferred) — Multiple recent-event paragraphs

Some news cycles have multiple events (e.g. Mark Carney: trade war
AND China deal AND Israel position). Today we pick the highest-
scoring one. M6 explores using 2-3 paragraphs as input. Defer until
we see the single-paragraph approach's quality bar.

---

## Risks

- **Wikipedia hasn't updated yet.** Event broke 1 hour ago; the
  article hasn't been edited. Specificity gate fails; chip falls off
  the rail. The chip will reappear on the next cycle once an editor
  catches up. Latency tradeoff for quality. Acceptable.

- **Wikipedia article has *historical* "On 5 May 2024" content that
  doesn't reflect why the article is trending now.** Mitigation:
  `EVENT_FRESHNESS_DAYS = 14` requires the most recent date in the
  paragraph to be within 14 days. So old prose with "On 12 March
  1994, …" doesn't qualify. Old prose with "On 12 March 2025, …"
  *does* — but if the article is trending now, that date is more
  likely "anniversary of" content or recent-event-from-perspective.
  Still acceptable.

- **Wikipedia API rate limits.** 100-300 candidates × 8 fetches/day =
  800-2400 API calls/day. Wikimedia's published anonymous limits
  are 200/sec; we're well within. Bot policy requires UA with a
  contact, which we already have.

- **Mac Mini queue saturation.** News jobs add ~10-30 jobs per cycle
  on top of biographical jobs. At 2-4 min/job that's 0.5-1.5 hours
  of Mac Mini time per 3-hour window. Should fit. If we routinely
  hit the wall, reduce biographical-job cadence or move biographical
  generation to a slower cycle.

- **The news prompt makes the model write fiction.** Smaller models
  could hallucinate events not in `event_paragraph` if poorly
  prompted. Mitigation: the prompt rule "Use only facts in the
  source paragraph below" + parse_summary's existing word-count
  rejection. We accept that 5-10% of news summaries get rejected and
  retried; that's already the existing pattern for biographical
  jobs.

- **A spike disappears mid-summary-generation.** Mac Mini is
  generating a Mark Carney news summary; by the time it lands, Mark
  Carney is no longer on the rail. The summary still goes into the
  FlatStore under `mark carney:news`; the server logic only serves
  it if the query is *currently* spiking. So a stale summary sits
  unused until either:
  (a) Mark Carney spikes again (it gets served — but if the news
      moved on, it's wrong)
  (b) compaction deletes it after 30 days
  Mitigation: at-serve-time, check `event_date` against
  `STALE_NEWS_DAYS_SERVE = 14`. Don't serve a news summary whose
  paragraph is older than two weeks even if the query is spiking
  again. Better to fall through to biographical.

---

## Open questions

- **Threshold tuning.** `date_specificity >= 4`, `EVENT_FRESHNESS_DAYS
  = 14`, `STALE_NEWS_DAYS_SERVE = 14`, `RAIL_MAX = 12`. All
  hand-picked. We'll need a day of live data to see whether 4 is too
  strict (too few qualifying paragraphs) or too lax (Trisha Krishnan
  style false positives slip through).

- **Article-specificity gate could promote pageview noise.** If the
  gate is the *primary* filter, dropping `SHAPE_PREV_MIN_RATIO` to 1.0
  (i.e. removing the temporal shape filter entirely) and relying on
  specificity might give cleaner results. Counter-argument: pageview
  shape catches bot-traffic articles that happen to have a dated
  paragraph in them. Keep both filters; specificity is the
  *content* signal, shape is the *traffic* signal.

- **What about non-news spikes that are still interesting?** E.g.
  "Bohemian Rhapsody" trending because of a TV ad. No Wikipedia
  editor adds "On 9 May 2026 the song was featured in…" so it fails
  specificity. The chip falls off. That's the right behaviour for a
  knowledge-panel system trying to explain news; it's the wrong
  behaviour for a general-purpose "trending" rail. Open question for
  later: maybe we want two separate UI affordances — "Trending news"
  with specificity-gated content, and "Popular searches" with raw
  pageview-shape spikes. Not in scope for this PRD.

- **i18n.** Specificity scoring uses English month names. For now we
  only fetch en.wikipedia, so this is fine. Anything multilingual is
  a future PRD.

## Known behaviours (not bugs)

- **Ambiguous titles drop off the rail.** Wikimedia attributes
  pageviews to whatever article the user actually lands on. If
  "Euphoria" spikes because of the HBO show but readers initially
  land on `Euphoria` (the article about the emotion), our pipeline
  tracks the emotion article. That article has no recent dated event
  paragraph → gate rejects it → chip falls off. This is correct
  behaviour: the spike isn't a clean signal that the show is making
  news (it's a signal that the *term* is being searched, which is
  weaker). The chip would correctly appear if `Euphoria_(American_TV_series)`
  itself were spiking. Considered "follow disambig pages" as a
  mitigation; decided to accept the current behaviour rather than
  add complexity for a noisy signal.
