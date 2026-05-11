# PRD-020: Trending Pages — Spiking Signal for Homepage and Ranking

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-10

---

## Problem

The homepage is static: search box, no other content. New visitors get no sense
that the site is alive — that there's an index behind it, that other people
are searching for things, that today's hot topic is in here. Google's
homepage now does this badly, but it used to do it well (and the Zeitgeist
page was the canonical "look how alive this index is" surface). Microsoft's
xRank (briefly) did the same thing. We want a small version of that
freshness signal under the search box.

The obvious approach — show the most-viewed Wikipedia articles each day —
fails on the perennials. The top-1000 pageviews list on any given day is
roughly:

1. `Main_Page` (always)
2. `Special:Search` (always)
3. `Cleopatra` (always, weirdly)
4. `Adolf_Hitler` (always)
5. `Elizabeth_II` (perennial)
6. `Donald_Trump` (perennial)
7. `Deaths_in_2026` (always, by month)
8. ... maybe-something-actually-fresh at rank 30+

Showing the raw top-N would be both boring (same content every day) and
slightly grim (Hitler on the homepage every day). The signal we want is
**spiking** — articles whose pageview count is unusually high *relative
to their own baseline*. That's the same signal we'd want from a ranking
boost later: "Mark Carney" should rank above `Mark Carney_(disambiguation)`
the week he becomes PM, not in general.

Secondary problem: daily resolution misses breaking news. By the time a
3 AM UTC daily job runs, an event that broke at noon yesterday is 15 hours
stale, and an event that breaks at 4 AM today won't surface until tomorrow.
Multi-hour cadence (every 3-6 hours) catches the same-day spike without
adding much load.

---

## Goal

Build a **trending-pages pipeline** that pulls Wikipedia pageview data
on a multi-hour cadence and produces a small ranked list of articles
whose pageview counts are spiking relative to their own baseline. Surface
the top of that list as a chip rail on the homepage. Persist the full
ranked list (with scores) for future use as a freshness boost in ranking.

Day-1 fallback: if we don't yet have enough history to compute a spike
score, render the raw top-N (filtered for junk). After ~7 days of
accumulated history, switch automatically to the spike score.

---

## Non-goals

- **Real-time pageviews.** Wikimedia's EventStreams firehose would let us
  go realtime, but the engineering cost (keeping a long-running consumer,
  aggregating in-memory, dealing with backpressure) is far above the
  freshness benefit. 3-6 hour cadence is enough.
- **Personalisation.** No "trending for you". Same list for every visitor.
- **A full Zeitgeist page.** Just the chip rail on the homepage. The full
  page is a separate PRD, later.
- **Live ranking boost.** Captured as a future M-milestone; the data layer
  this PRD builds is designed to feed it, but plugging into the BM25
  scoring path is out of scope here.
- **Tracking individual user clicks on chips.** Existing click logging
  picks this up for free via the `?q=` URL parameter, no special handling.

---

## High-level design

```
                      Wikimedia REST API
                  (every 3-6 hours via timer)
                              │
                              ▼
                  tools/fetch_trending.py
              ┌───────────────────────────────┐
              │  • pull top-1000 for the      │
              │    most recent complete hour  │
              │    bucket (or day, see below) │
              │  • filter denylist            │
              │  • normalise titles → queries │
              │  • append to history          │
              │  • compute spike score        │
              │    (or fall back to raw rank) │
              └───────────────┬───────────────┘
                              │
                              ▼
              /mnt/wikipedia-source/trending/
                ├── history.jsonl   (append-only, 30-day window)
                ├── current.json    (latest ranked list, atomic write)
                └── fetch.log
                              │
                              ▼
                  server.py /api/trending
                  (reads current.json, mtime-cached in memory)
                              │
                              ▼
                  index.html homepage chip rail
                  (8 chips under the search box;
                   click → ?q=<query>)
```

A second consumer (future): a process that loads `current.json` into a
title→score map at index time, sidecars it next to the docmap, and the
ranker adds a small additive boost. Out of scope for now but the data
shape is chosen so this plugs in cleanly later.

---

## Cadence and source

Wikimedia publishes pageviews two ways:

1. **REST API** `/metrics/pageviews/top/en.wikipedia/all-access/{Y}/{M}/{D}` —
   daily aggregate, ~24h lag. JSON, no key, rate-limited generously.
2. **Hourly dumps** `dumps.wikimedia.org/other/pageviews/{Y}/{Y-M}/pageviews-{YMD}-{HH}0000.gz` —
   raw per-page hourly counts, ~30-90 min lag. Gzipped TSV, ~50 MB per
   file, includes all projects (we filter to `en`).

**Decision: hourly dumps.** They're 50 MB compressed but we only need to
unzip, filter to project=`en`, sort by count, keep top 1000. ~5 seconds
of work. Running the fetch every 3 hours gives 8 samples per day, enough
to detect a same-day spike. The REST API is simpler but loses the
within-day signal that's the whole point of moving off daily.

**Default schedule:** every 3 hours, on the hour offset by 30 minutes
(`*:30`) to give the dumps time to land. Configurable via the timer
unit; setup.sh installs it.

---

## Spike scoring

For each article we keep a 30-day rolling history of pageview counts at
the same granularity we fetch them. Call this `views[a, t]` for article
`a` at sample time `t`. The score at the current sample `t0` is:

```
score(a) = log( (views[a, t0] + smoothing) / (median(views[a, t-30d..t-1]) + smoothing) )
```

with `smoothing = 100` to keep the ratio sane for low-volume articles
that suddenly spike from 0 to 50 views per hour. Articles with no
history at all (new pages) fall back to their raw rank.

Why log: a 10× spike and a 100× spike should both look "trending" but
the 100× shouldn't dominate the rail. Log compresses.

Why median, not mean: medians are robust against earlier spikes for the
same article. If `Mark_Carney` spiked last week and is now back to
normal, the median is the normal level, so a new spike this week is
detected; with a mean the earlier spike would inflate the baseline and
hide this week's.

Articles with `score < log(2)` (less than 2× their median) don't qualify
as "trending" and are dropped from the rail even if they'd rank top-8.
The rail then back-fills from the next-best spikers. If fewer than 4
articles qualify, we fall back to filling the rail with raw top-N
(excluding denylist) — better to show something than an empty rail.

---

## Day-1 fallback (no history yet)

For the first 7 days after the pipeline starts, we don't have a 30-day
window. During this period:

- Score = `null`
- Rail renders raw top-N, denylist-filtered, in descending pageview order
- After 7 days of samples accumulated, the median is meaningful and we
  switch to spike scoring automatically

This is the "we just started, here's what's hot today" experience.
After a week it improves to "here's what's spiking right now".

The switchover is data-driven: the scorer checks how many samples it
has for each article and only computes a spike score for articles with
≥ 21 samples (7 days × 3 samples/day). Articles below threshold fall
through to raw rank.

---

## Denylist

Filtering applied before scoring:

```
^Main_Page$
^Special:.*
^Wikipedia:.*
^File:.*
^Portal:.*
^Help:.*
^Category:.*
^Template:.*
^User:.*
^Talk:.*
.*_talk:.*

^\d{4}$                     # year articles
^\d{4}_in_.*                # "2026 in film"
.*_in_\d{4}$                # "Film in 2026"
^Deaths_in_\d{4}            # ugh
^Births_in_\d{4}
^List_of_.*                 # most "List of..." pages are perennial junk
^Lists_of_.*

# Adult-content + shock pages we don't want on the homepage even if hot
(case-insensitive partial match against a small explicit list, configurable)
```

The adult-content sublist is intentionally small and lives in a separate
file (`tools/trending_denylist.txt`) so it can be edited without code
changes. Same for the structural patterns — they're a constant in
`fetch_trending.py` but easy to amend.

---

## Title → query normalisation

Wikipedia article titles are URL-form: `Sundar_Pichai`,
`2026_Australian_federal_election`, `Apple_Inc.`. The query a human
would type to find them is `sundar pichai`, `2026 australian federal
election`, `apple inc` (probably without the period).

Normalisation:
1. Replace `_` with space.
2. Lowercase.
3. URL-decode (handles `%26` etc.).
4. Strip trailing `.`, `,`, `?`, `!`.
5. Drop parenthesised disambiguators: `Mercury_(planet)` → `mercury`.
   (We accept that this maps multiple articles to the same query — the
   ranker will pick the right one, and that's the whole point.)

Two articles can normalise to the same query (e.g. `Mercury_(planet)`
and `Mercury_(element)` both → `mercury`). When this happens, we keep
the highest-scoring of the collisions and dedupe.

---

## Data shapes

### `history.jsonl` (append-only)

One line per sample, gzipped after 7 days:

```json
{"t": "2026-05-10T12:00:00Z", "rows": [["Sundar_Pichai", 14223], ["Apple_Inc.", 8910], ...]}
```

Only top-1000 per sample is retained. Median computation reads the last
30 days of these lines.

### `current.json` (atomic-written each fetch)

```json
{
  "generated_at": "2026-05-10T12:34:56Z",
  "sample_window": "2026-05-10T09:00:00Z..2026-05-10T12:00:00Z",
  "mode": "spike",                       // or "raw" during day-1 window
  "items": [
    {
      "query": "mark carney",
      "title": "Mark_Carney",
      "rank": 1,
      "views": 14223,
      "median_baseline": 412,
      "score": 3.54                      // null in raw mode
    },
    ...
  ]
}
```

`items` is sorted by `score` desc in spike mode, `views` desc in raw mode.
Length is whatever passes the qualifying threshold, capped at 50.

### `/api/trending` (HTTP)

```
GET /api/trending?n=8
```

Returns:

```json
{
  "mode": "spike",
  "generated_at": "2026-05-10T12:34:56Z",
  "items": [
    {"query": "mark carney", "title": "Mark Carney"},
    {"query": "australian election 2026", "title": "2026 Australian federal election"},
    ...
  ]
}
```

The server-side endpoint reads `current.json` and projects to just
`{query, title}` for the rail — the homepage doesn't need scores. mtime
cache: re-read the file only when its mtime changes.

If `current.json` doesn't exist, return `{"items": []}` and the chip
rail simply doesn't render. The homepage degrades silently — never
breaks because trending isn't ready.

---

## Frontend (homepage chip rail)

Position: directly under the homepage search box, above the empty space.
Not on the results page (the results themselves are the dynamic content
there).

Visual: 8 small pill-shaped chips with the article title (not the
normalised query — `Mark Carney` is nicer than `mark carney` to look
at). Light grey background, subtle hover. Click navigates to
`/?q=<encoded_query>` which already triggers a search.

Empty/error states:
- `items` empty → don't render the rail at all (no "no trending yet"
  message; just no chips).
- `/api/trending` fails → same; silent.

The rail itself loads async after the page paints — we don't block on it.

---

## Future hooks (ranking boost — separate PRD)

The data shape supports a future ranking integration: at index build or
hot-reload time, read `current.json`, build a `Map<normalised_title,
score>`, and the BM25 scorer adds `α * clamp(score, 0, 4)` to the final
score. α tunable; clamp avoids extreme spikes (a 1000× spike shouldn't
shove an irrelevant page to rank 1).

The `current.json` format includes `score` for every item explicitly so
that future ranker plumbing doesn't need to recompute. The fetch script
already does the work.

Not in scope here — flagged so we don't repaint the data shape later.

---

## Milestones

### M1 — Fetcher + storage (day 1)

- `tools/fetch_trending.py` — pulls one hourly dump, filters,
  normalises, appends to `history.jsonl`, computes `current.json`.
  Mode = `raw` (no spike yet — single sample).
- `tools/trending_denylist.txt` — initial list.
- Systemd timer + service unit, every 3 hours offset 30 min.
- `setup.sh` installs both.
- Smoke test: run once, confirm `current.json` exists and looks sane.

### M2 — Server endpoint (day 1)

- `server.py` adds `GET /api/trending`.
- Mtime-cached read of `current.json`.
- Returns `{items: []}` on missing file, no error.

### M3 — Homepage chip rail (day 1)

- `index.html` — chip rail under the search box.
- Async fetch on page load.
- Hidden when empty.
- Click → `/?q=<query>` (existing search flow handles the rest).

### M4 — Spike scoring (after ~7 days of samples)

- `fetch_trending.py` switches mode to `spike` once it has ≥21 samples
  for ≥10 articles.
- Scorer reads 30-day history, computes log-ratio per article.
- Drops items with score < log(2).
- Falls back to filling with raw top-N if < 4 qualify.
- No code or config change needed — the switchover is automatic; we
  just have to wait for the data to accumulate.

### M5 — History compaction (after ~30 days)

- Old samples (>30 days) are trimmed from `history.jsonl`.
- Optionally archive to `history-YYYY-MM.jsonl.gz`.
- A `compact` subcommand on `fetch_trending.py`, run weekly.

### M6 (deferred) — Ranking boost

Separate PRD. The data this builds is the input; the ranker plumbing
is the work.

### M7 (deferred) — Full Zeitgeist-style page

`/trending` page with longer list, charts, week-over-week. Separate PRD.

---

## Risks

- **Dump file shape changes.** Wikimedia have changed pageview formats
  before (the move from `pagecounts-raw` to `pageviews-`). We assume
  the current format is stable; if it changes, the parser breaks and
  the rail goes empty. Mitigation: server returns empty items, chip
  rail hides itself, no user-visible breakage.
- **Junk leaks through denylist.** The first week of running will
  surface things we didn't think to filter (regional `Main_Page`
  variants, `Wikipedia:Reference_desk`, etc.). The denylist is plain
  text and edited frequently in M1-M2.
- **Spike scoring promotes weird things.** A list page that's normally
  10 views/day spiking to 200 still passes the spike threshold but is
  not a useful chip. Mitigation: `List_of_*` is denylisted; we'll add
  others as they appear.
- **Mac Mini summariser doesn't yet know about trending queries.** When
  the rail surfaces a query that has no summary, the knowledge panel
  falls back to the snippet — which is fine. Optionally, a future hook:
  M1 also pushes the top-50 trending queries into the summariser's
  pending queue so the next sweep starts producing summaries for them.
  Out of scope for this PRD; captured here so we remember.

---

## Open questions

- **Hourly resolution vs 3-hourly.** 8 samples/day is enough for spike
  detection. Hourly is 24 samples/day, smoother medians, but 8× the
  storage and request rate against dumps.wikimedia.org. Default
  3-hourly; can drop to hourly later if scoring is noisy.
- **All-access vs desktop-only.** The REST API and dumps both have
  variants. We default to `all-access` (desktop + mobile + app) since
  that matches user interest. Worth A/B comparing once we have data.
- **English-only.** First version is `en.wikipedia` only. Multilingual
  is out of scope.
