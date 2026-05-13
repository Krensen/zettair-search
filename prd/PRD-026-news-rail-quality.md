# PRD-026: News-Rail Quality — Better Filtering + Editor-Curated Sources

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-13

---

## Problem

Today's homepage rail is a mix of real news, gossip, marketing, and
stale content. Live snapshot at the time of writing this PRD:

| Spike | What it is | Verdict |
|---|---|---|
| Wes Streeting | UK Labour leadership challenge | Real news ✓ |
| Eurovision Song Contest 2026 | First semi-final qualifiers | Real news ✓ |
| C. Joseph Vijay | New Chief Minister of Tamil Nadu | Real news ✓ |
| The Punisher: One Last Kill | Disney+ special, behind-the-scenes feature | Marketing ✗ |
| The Boys season 5 | "Episode 7 release date" recap | Marketing ✗ |
| Michelle Trachtenberg | "Sarah Michelle Gellar remembers… one year on" | Stale obit ✗ |
| XXX: State of the Union | 2005 movie, top "headline" is IMDb actor page | Pure noise ✗ |

3 of 7 are useful. We can do better at both ends:

1. **Filter quality**: noise (marketing, stale obituaries, IMDb scrapes
   masquerading as news) leaks through the existing specificity gate.
2. **Candidate coverage**: the rail is *missing* major news of the day
   because the spike filter only catches articles whose pageviews
   delta-spike — and most major news (Trump, Carney, Fed rate decision,
   Israel-Iran) lives on perennially-popular articles whose baseline
   already swamps any spike.

The mental model has been "find pageview spikes, validate they're
news". It should be "find news, augment with detected spikes". This
PRD reframes the rail by adding two editor-curated sources alongside
the spike pipeline, then applying a stricter quality filter to the
union.

---

## Goal

Three coordinated changes:

1. **Add Google News top-stories** as a parallel candidate source.
   The trending fetch pulls Google News's top-stories RSS each cycle,
   maps headline subjects → Wikipedia docnos, and adds those to the
   candidate pool. No spike-filter required for these — Google News
   editors have already done the work.

2. **Add Wikipedia "In the news" portal** as a parallel candidate
   source. Same idea, Wikipedia-curated. Less timely but more
   rigorously selected, and the entities are already Wikipedia
   docnos so no normalisation is needed.

3. **Tighten the quality filter** applied to the union of all three
   sources (spike + Google + Wikipedia). New rules:
   - Top Google News headline must be ≤7 days old.
   - At least one of the top-3 headlines must come from a recognised
     mainstream news outlet.
   - Marketing-pattern headlines drop the candidate (with extended
     pattern set: "behind the scenes", "season N episode N",
     "ending explained", etc).
   - Stale obituary: `YYYY deaths` Wikipedia category + obit-flavoured
     top headline + headline >30 days old → drop.

Net effect targets:
- 10-15 chips on the rail (was 4-8), 95% of them real news (was ~50%).
- Major-news coverage matches what an attentive reader would expect:
  Trump, Carney, Eurovision, Streeting, plus genuine spikes the
  pipeline catches that editors might miss (Tristan da Cunha-class).

---

## Non-goals

- **Fix 3 (broader-corpus baseline scoring)**. Acknowledged as a real
  improvement but deferred — the candidate sources above probably
  cover most of the gap. Revisit if we still see major-news misses
  after PRD-026 lands.
- **Personalisation**. Same rail for every visitor.
- **Multilingual**. en.wikipedia and US-English Google News only.
- **Ranking the rail by relative newsworthiness**. We rank by spike
  score for spike-source items, by source-order for Google News
  top-stories (Google's own ranking), and by editor-order for
  Wikipedia In-the-news. Cross-source ranking is out of scope; we
  interleave deterministically (see "Source weighting" below).
- **Live news fetching at search time**. All work is offline in the
  3-hour trending cycle.
- **Replacing the spike pipeline**. The spike signal still catches
  things editors miss (Tristan da Cunha) — it's a complementary
  source, not a deprecated one.

---

## High-level design

```
Every 3 hours via zettair-trending.timer:

  ┌─────────────────────────────────────────────────────────────┐
  │ STAGE A — gather candidates from three sources              │
  │                                                              │
  │  Source 1: pageview-spike pipeline (PRD-020 + PRD-021)      │
  │    - top-3000 hourly dump → shape filter → ~10 candidates   │
  │                                                              │
  │  Source 2: Google News top stories (NEW)                    │
  │    - GET news.google.com/rss?hl=en-US&gl=US                 │
  │    - extract entity subjects from each headline             │
  │    - normalise to Wikipedia docnos                          │
  │    - ~30-50 candidates                                      │
  │                                                              │
  │  Source 3: Wikipedia "In the news" portal (NEW)             │
  │    - GET en.wikipedia.org/wiki/Portal:Current_events/<date> │
  │    - parse [[Article]] links from each event blurb          │
  │    - ~10-20 candidates                                      │
  │                                                              │
  │  UNION + dedupe by docno                                    │
  │  → 40-70 candidate docnos                                   │
  └─────────────────────────────────────────────────────────────┘
                                │
                                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ STAGE B — quality filter (NEW)                              │
  │                                                              │
  │  For each candidate, fetch Google News headlines (cached    │
  │  per PRD-022's 6h cache). Apply filters in order:           │
  │   1. Reject stale-obituaries (dead-year + obit + >30d)      │
  │   2. Reject marketing-pattern headlines                     │
  │   3. Require top headline ≤7d old                           │
  │   4. Require ≥1 mainstream source in top 3                  │
  │                                                              │
  │  Surviving candidates also need an event_paragraph for the  │
  │  news summary — either from Wikipedia (PRD-021's gate)      │
  │  or synthesised from the cached headlines (PRD-022). When   │
  │  the candidate came from Source 2 or 3, the headlines       │
  │  themselves are already the event source — no Wikipedia     │
  │  fetch needed.                                              │
  └─────────────────────────────────────────────────────────────┘
                                │
                                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ STAGE C — write current.json                                │
  │                                                              │
  │  items[] interleaved by source weighting (see below).       │
  │  Each item carries source ∈ {spike,google_news,wiki_itn}    │
  │  for debugging/telemetry.                                   │
  └─────────────────────────────────────────────────────────────┘
                                │
                                ▼
  Downstream unchanged: news-summary producer, Mac Mini, server,
  homepage rail. Already source-agnostic since PRD-022.
```

---

## Pieces

### 1. Google News top-stories fetcher

```
GET https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en
```

Returns ~50 current top stories as RSS, each with `<title>`,
`<link>`, `<pubDate>`, `<source>`. Each `<link>` is a Google News
URL that opaquely wraps the actual article. The title typically
has the form `"Headline - Source"`.

**Subject extraction.** The hard part. We need to map a headline to
one or more Wikipedia docnos so we can put them on the rail. Approach:

- Run a quick named-entity pass over the headline using a hand-built
  lookup table. Pre-built at index-rebuild time: for each Wikipedia
  entity article (PRD-025's entity classification!) we have a docno
  + a canonical title. Build a reverse map from `lower(title)` →
  docno. Then for each headline, look for token-matches against
  this map.
- Limit to entities of class human / organisation / place / event
  (PRD-025 classes). Filter out single-word common-words to avoid
  false matches.

Example: headline "PM has 'full confidence' in Streeting" matches
on "Streeting" → `Wes_Streeting`. Headline "Trump meets Carney to
discuss tariffs" matches on "Trump" → `Donald_Trump` *and* "Carney"
→ `Mark_Carney` — we add both as separate candidates.

Storage: a TSV `/mnt/wikipedia-source/related/entity_titles.tsv`
built alongside `entity_classes.json` by an extension to
`build_entity_set.py`. Loaded by `fetch_trending.py` at fetch time
into a dict. Memory: ~30 MB; lookup is O(1).

**Cache.** Same 6h cache strategy as PRD-022's headlines. Single
top-stories pull per cycle, cached at
`/mnt/wikipedia-source/trending/google_top_cache.json`.

### 2. Wikipedia In-the-news portal fetcher

```
GET https://en.wikipedia.org/wiki/Portal:Current_events/<YYYY_Month_DD>
```

Returns an HTML page with the day's events. Format is consistent:
each event is a bullet list item containing one or more
`[[Wikipedia_article]]` links. We just need to extract the linked
docnos.

Even easier: Wikipedia has a structured JSON dump of the same data
via the REST API:

```
GET https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/<MM>/<DD>
```

But that's *historical* events for today's date, not today's news.
The right endpoint for current news is:

```
GET https://api.wikimedia.org/feed/v1/wikipedia/en/featured/<YYYY>/<MM>/<DD>
```

Returns a `news` array with linked articles. Each item has
`story` (HTML blurb) + `links` (array of full Wikipedia objects with
`titles.canonical` = docno).

Cache: 24h (Wikipedia's portal updates daily; refetching every 3h
is wasteful). File: `/mnt/wikipedia-source/trending/wiki_itn_cache.json`.

Expected candidates: 5-15 per day. Small but high-quality.

### 3. Stricter quality filter

Implemented in `apply_specificity_gate` (or a new
`apply_quality_filter` that runs after the specificity gate).

Order matters; first failure wins:

```python
def quality_check(candidate, headlines):
    # 1. Stale obituary
    if candidate.is_dead and any(OBIT_RE.search(h.title) for h in headlines[:3]):
        if headlines and (today - headlines[0].pub_date).days > 30:
            return False, "stale obituary"

    # 2. Marketing pattern in headlines
    if any(MARKETING_RE.search(h.title) for h in headlines[:3]):
        return False, "marketing pattern in headlines"

    # 3. Top headline must be ≤7 days old
    if not headlines or (today - headlines[0].pub_date).days > 7:
        return False, "no recent (<=7d) headline"

    # 4. Require a mainstream source in top 3
    if not any(MAINSTREAM_RE.search(h.source) for h in headlines[:3]):
        return False, "no mainstream source in top 3"

    return True, "ok"
```

Constants:

- `OBIT_RE`: `died|death|passed away|obituary|remembered|tribute|anniversary of (his|her) death|posthumous`
- `MARKETING_RE`: `trailer|teaser|poster|release date|cast announced|first look|premieres|opening weekend|box office|behind the scenes|featurette|special presentation|streaming now|now streaming|new episode|season \d+ episode|episode \d+|recap|review|easter eggs|ending explained|finale|how to watch|where to watch`
- `MAINSTREAM_RE`: `bbc|reuters|new york times|nytimes|the times|the guardian|washington post|associated press|\bap\b|bloomberg|the independent|financial times|\bft\b|the hindu|the economist|al jazeera|cnn|npr|abc news|cbs news|nbc news|sky news|the telegraph|the times of india|the wall street journal|wsj|the atlantic|politico|axios|propublica|the conversation`

All patterns are tunable env vars later if we want; v1 hard-codes.

**`is_dead` detection.** Cheap: hit Wikipedia API
`prop=categories&cllimit=30` for each candidate, look for
`Category:YYYY deaths`. Cached at 24h per docno.

### 4. Source weighting in current.json

When we have N total candidates surviving the quality filter, how do
we rank them on the rail? Three-source interleave, weighted:

- **Google News top-stories**: most timely + editor-curated → highest weight
- **Spike pipeline**: catches the niche stories editors miss → medium
- **Wikipedia In-the-news**: rigorous but daily-cadence → medium

Implementation: tag each item with `source` and `source_rank`. Final
sort key:

```python
def sort_key(item):
    source_priority = {"google_news": 0, "spike": 1, "wiki_itn": 2}
    return (source_priority[item.source], item.source_rank)
```

This gives a deterministic interleave that prefers Google News while
including spike and wiki-itn items. Cap total at `RAIL_MAX = 12`.

### 5. Telemetry and observability

Each item in `current.json` gets new fields:

- `source`: `"spike" | "google_news" | "wiki_itn"`
- `source_rank`: int (position within its source's list)
- `filter_pass`: list of strings naming which filters it passed
- `top_headline`: object with title/source/age, kept for debugging
  and the news-summary generation

In the fetch log we now print per-source counts:

```
sources: spike=8 google_news=42 wiki_itn=14
union: 51 unique candidates
quality filter: dropped 38 (stale_obit=2 marketing=12 stale_news=8 non_mainstream=16)
final: 13 items
```

### 6. Frontend (mostly unchanged)

The rail still renders each item as a chip. Two additions:

- The "Trending" rail label could become "**News**" since we're no
  longer purely a spike rail. Open question.
- For Google-News-sourced items where we have no Wikipedia article in
  our corpus, the chip behaves like the `in_index: false` case from
  PRD-020 — direct Wikipedia link with a different icon.

---

## Milestones

### M1 — Stricter filter (~half-day)

- Add the four filter functions to `fetch_trending.py`.
- Wire into `apply_specificity_gate` or a new `apply_quality_filter`.
- Add `is_dead` lookup via Wikipedia categories API with 24h disk cache.
- Update logs to include filter-rejection counts.

Tested locally against the live snapshot; expected to drop 4/7 of
today's candidates (Punisher, Boys, Trachtenberg, XXX) cleanly.

### M2 — Google News top-stories (~1 day)

- New `fetch_google_top_stories()` in `fetch_trending.py`.
- Build the title→docno reverse map by extending
  `build_entity_set.py` to emit `entity_titles.tsv` alongside
  `entity_classes.json`.
- Headline → candidates extraction with the entity title map.
- 6h cache.
- Wire into the candidate pool, source-tag each item.

### M3 — Wikipedia In-the-news portal (~half-day)

- New `fetch_wikipedia_itn()` in `fetch_trending.py`.
- Pull JSON from the wikimedia feed REST API.
- Extract `news.links[].titles.canonical` per story.
- 24h cache.
- Wire into candidate pool, source-tag.

### M4 — Source weighting + frontend tweaks (~half-day)

- Sort items by source priority + source rank.
- Cap at RAIL_MAX = 12.
- Frontend: switch rail label to "News" (open question — keep
  "Trending"?), reuse existing in-index / wiki-link logic.

### M5 — Observe and tune (1 day, plus a week of watching)

- Watch for a week. Tune marketing-pattern regex, mainstream-source
  list, source-priority weighting.
- Add a small `tools/inspect_trending.py` that snapshots the
  current state with per-item filter decisions, for quick
  debugging.

### M6 (deferred) — Broader-corpus baseline scoring (Fix 3 from the
analysis)

If we still see major-news misses after M1-M4 (e.g. articles where
the editor sources don't pick them up but they're clearly news),
implement percentile-based spike scoring against the global
view distribution. Future PRD.

---

## Risks

- **Google News title-to-entity matching is noisy.** "Trump" alone
  matches dozens of articles in our docno set. Mitigation: prefer
  multi-word matches, prefer exact title matches, and use PRD-025's
  entity classification to keep matches in (human/place/org/event)
  space. Some false positives expected; the quality filter catches
  most.

- **Mainstream-source allowlist is biased.** Hand-curated; will miss
  regional or specialist news outlets. Mitigation: not blocking on
  this — most news has mainstream coverage somewhere in top 3. If
  we see legit news missed, expand the list.

- **Wikipedia In-the-news may surface duplicates.** Same story can
  link to multiple Wikipedia articles (event + actor + place).
  Mitigation: dedupe within sources; spike-pipeline takes precedence
  on cross-source dupes.

- **Google News rate limits.** Top-stories pull is one extra request
  per 3h cycle; we're well within rate. Worst case: the call fails
  and we fall back to spike-only behaviour.

- **Wikipedia portal API may not be quite the right endpoint.** Need
  to verify `/feed/v1/wikipedia/en/featured/<date>` returns current
  news rather than today's anniversaries. v1 should confirm with a
  one-off test before committing.

- **The rail is now 95% news, but does it still represent the
  spike-driven discovery aspect?** Some of the most interesting
  things on the rail were obscure (Tristan da Cunha medical evac).
  Spike is a third of the input; it'll get represented via the
  source-priority weighting. Tune if it drops out.

- **The filter is harsher than the existing one.** Today: 7 chips
  (mixed quality). After M1 alone: 3 chips. After M1+M2+M3: target
  10-15. If M2/M3 underperform we're stuck at 3-5, worse than
  today's count. Acceptable trade for quality, but worth watching.

---

## Open questions

- **Rail label.** "Trending" no longer captures it once we add
  editorial sources. Options:
  - "News" — simple, matches what most users would call it.
  - "Trending news" — keeps the trending word.
  - Same "Trending" as today — least change, least informative.

  Lean: **"News"**, simple wins.

- **Should Google-News-only candidates (no Wikipedia article in our
  corpus) appear?** They give us coverage of stories whose Wikipedia
  article we don't index. The existing `in_index: false` path
  handles them (external Wikipedia link). Probably yes; keeps the
  rail dense.

- **Marketing-pattern regex calibration.** "Review" is a strong
  marketing signal but also legitimate news ("Pentagon review finds
  X"). Need to look at false-positive rate in practice. v1 conservative
  pattern; tune in M5.

- **Multiple articles per Google News story.** "Trump meets Carney"
  generates two candidates. Should they appear as two chips or one
  combined "Trump + Carney"? v1 says two — easier to render and the
  user can pick. Revisit if this looks bad.

- **Marketing for *actual* news stories.** A film festival winner's
  Wikipedia article might spike legitimately for the award win, but
  Google News headlines could trip the marketing filter ("Cannes
  Film Festival 2026 review"). False positives expected; tune in M5
  once we have data.
