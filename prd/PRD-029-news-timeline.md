# PRD-029: News Timeline — "The News, According to Wikipedia"

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-25

---

## Problem

zettair.io is a good search engine over Wikipedia. It is not yet a
reason to come back when you are not looking for something specific.

The product surfaces today around a search box; the trending rail is a
small affordance under it. A user with a search query has a clear path.
A user who is curious about the world has no path at all — they have to
*know* what to search for. Newspapers solve this differently: they
arrange today's events into a front page. So does the Wikipedia "In the
news" portal, but it is buried in the sidebar of a 30-year-old
information-architecture and almost nobody reads it.

There is a real opening here that nobody is filling well:

- **Newspapers have agendas.** Even good ones tilt; bad ones manipulate.
  Readers know this, distrust it, and increasingly bounce between
  sources to triangulate. The triangulation is exhausting.
- **Wikipedia has process, not editors.** Its neutrality is not the
  absence of bias but the public visibility of bias-correction. By the
  time an event lands in a Wikipedia article paragraph, hundreds of
  edits have already filed off its sharp partisan edges. The output is
  flatter, slower, and more trustworthy — at the cost of being a few
  days behind.
- **Wikipedia hides this strength.** The reader who would love
  "newspaper-shaped, but neutralised through Wikipedia's editing
  process" has no UI for it.

We already have most of the data substrate. PRD-021's specificity gate
extracts dated event paragraphs from Wikipedia articles for every
trending entity, and `event_date` + `event_paragraph` are persisted in
the trending pipeline. PRD-025 has classified ~730k entities by class
(human, place, organisation, work, event). PRD-026 unions Google News
top stories and Wikipedia ITN into the candidate set. Each ingredient
has been built for a different reason; together they are most of a news
timeline.

The strategic bet behind this PRD: **the difference between "Wikipedia
search" and "the way I read about the world" is presentation.** Same
data, repositioned, becomes a different product. Wikipedia's authority
is the moat; the timeline is the way to surface it.

---

## Positioning bet

There is a spectrum from "search engine with news on the side" to
"news app with search hidden." The four positions:

1. **Search engine with a news rail** — today. Search owns the page,
   news is a chip strip. Status quo.
2. **Search engine with a big news strip** — homepage adds a
   horizontal day-by-day strip below the search box. Same brand;
   news now twice the size.
3. **News + search, equal weight** — homepage is the timeline.
   Search is a sticky toolbar, one keystroke away (⌘K / `/`). New
   brand line: *"the news, as Wikipedia tells it."*
4. **News with search hidden** — search collapses to an icon.
   Repositions the product completely. Highest reward, highest risk.

**This PRD picks position 3.** Reasons:

- Position 2 is too small a bet to be worth a major redesign. It is
  a chip-rail expansion and we can do that anytime.
- Position 4 is a one-way door. Search is the only part of the
  product most users already understand; removing the affordance is
  a brand-and-bounce-rate gamble we should not take without
  evidence.
- Position 3 keeps search reachable from every screen but makes the
  default page a reason to come back. If it works, it works because
  users land on the timeline and read. If it does not work, search
  is one keystroke away and the regression cost is small.

Position 3 also gives us the option to evolve to position 4 later
without a second redesign — the toolbar just shrinks.

---

## Goal

A scrollable, zoomable timeline of the events Wikipedia has documented,
organised by date, with image-bearing cards and a consistent visual
identity per recurring entity. Home page. Search lives in a sticky
top-of-page toolbar.

Concretely: a user landing on `zettair.io` sees today's top events as
image cards, can scroll back through yesterday and the past week,
can zoom out to see months or years, and can recognise recurring
entities by colour (e.g. Iran's red appearing across multiple months
makes the pattern visible at a glance).

The bet is that this is the kind of homepage a user opens *because
they want to*, not because they have a specific query.

---

## Non-goals

- **Real-time breaking news.** Wikipedia is not faster than Twitter
  or news wires and we should not pretend otherwise. The product is
  about "what Wikipedia has documented," which lags reality by hours
  to days. This is a feature, not a bug. We will explicitly frame it
  as such in the UI.
- **Opinion / editorial content.** No "top stories of the week"
  human-curated lists. The whole pitch is that the curation is
  emergent from Wikipedia's editing process, not from us.
- **Personalisation.** Same timeline for every visitor in v1. A
  later PRD can add an opt-in personalised lens.
- **Comments / community features.** Wikipedia talk pages already
  do this and we are not replacing them.
- **A separate "Zeitgeist" page** as in the deferred PRD-020 M7.
  This subsumes that.
- **Backfilling event paragraphs from Wikipedia article histories.**
  In v1 we use what we have started capturing since PRD-021 went
  live. Backfill is a separate, larger pipeline question covered
  in "Open questions" below.
- **A second domain or app.** Same site, same domain.
- **Replacing the chip rail entirely.** The chip rail is a useful
  homepage affordance on `/`; this PRD's `/news` route is a new
  surface, additive at first.

---

## High-level design

```
┌──────────────────────────────────────────────────────────────────┐
│ Z   [  Search Wikipedia                          ]      ⌘K       │  ← sticky toolbar
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  News, as Wikipedia tells it                                     │
│  ─────────────────────────                                       │
│                                                                  │
│  Monday 25 May 2026                                              │
│      ●  ┌──────────┐ ┌──────────┐ ┌──────────┐                  │
│         │ Iran     │ │ OpenAI   │ │ Football │                  │
│         │ image    │ │ image    │ │ image    │                  │
│         │ summary  │ │ summary  │ │ summary  │                  │
│         └──────────┘ └──────────┘ └──────────┘                  │
│                                                                  │
│  Sunday 24 May                                                   │
│      ●  ┌──────────┐ ┌──────────┐                               │
│         │ Ukraine  │ │ Hong Kong│                               │
│         └──────────┘ └──────────┘                               │
│                                                                  │
│  Saturday 23 May                                                 │
│      ●  ┌──────────┐ ┌──────────┐ ┌──────────┐                  │
│         │ Iran     │ │ US polit │ │ Pep      │  ← same red       │
│         └──────────┘ └──────────┘ └──────────┘     as Mon        │
│                                                                  │
│           ⌄  scroll for older events  ⌄                          │
└──────────────────────────────────────────────────────────────────┘
```

Mock variants explored: `mockups/timeline-news-variants.html`. The
final layout is closest to variant **B** (vertical timeline with
image cards), drilling down on tap to a single-event detail view.
Variants C (newspaper bricks) and D (year strip) are deferred to
later zoom levels; the year strip is the killer "Iran every month"
visualisation but is heavier to build than the day timeline.

---

## Pieces

### 1. The `/news` route

A new route, additive. The existing `/` (search homepage) is not
changed initially.

`GET /news` renders the timeline page. URL state:

- `/news` — default; "today and the past 7 days"
- `/news/2026-05-25` — focused on a specific day
- `/news/2026-W21` — focused on a week (ISO week)
- `/news/2026-05` — focused on a month

Browser back/forward map to zoom-history. The page is server-rendered
HTML for the initial paint (so it is shareable and crawlable), with
client-side JS taking over for zoom transitions and scroll-driven
lazy load.

After ~30 days of soft-launch usage on `/news`, we evaluate whether
to promote it to `/`. This is the gate: if users on `/news` come
back at a meaningfully higher rate than users on `/`, we promote.
If not, we leave `/news` as an experiment.

### 2. Data substrate: the event index

The timeline reads from a new offline-built index:

```
events.jsonl   — one event per line, sorted by date desc
events.idx     — by-date lookup (date string → byte offset in events.jsonl)
```

Each event record:

```json
{
  "docno": "Iran",
  "title": "Iran",
  "event_date": "2026-05-25",
  "event_paragraph": "On 25 May, Iran announced ...",
  "source": "wikipedia",          // or "news_rss" (PRD-022 fallback)
  "captured_at": "2026-05-25T03:00:00Z",
  "image_url": "https://upload.wikimedia.org/...",
  "entity_class": "place",         // PRD-025 class
  "spike_score": 1.42,             // from trending pipeline if available
  "rank_hint": 7                   // estimated importance, see §5
}
```

The substrate is built by an offline job, `tools/build_events_index.py`,
which:

- Reads `trending/history.jsonl` (already has the spike samples and
  event paragraphs accumulated since PRD-021).
- Deduplicates `(docno, event_date)` pairs; when a paragraph has been
  edited and re-captured, keeps the latest.
- Joins to `_images_store` for the entity's article image.
- Joins to `entity_class.json` for the class label.
- Emits `events.jsonl` sorted by `event_date` desc, then by
  `rank_hint` desc.
- Builds `events.idx` (a binary by-date offset map; ~10 KB per year).

Runs once per day from a new systemd timer
`zettair-events-index.timer`. Idempotent — only appends events the
trending pipeline has captured since the last run.

Server loads `events.idx` at startup into memory; `events.jsonl` is
read via `os.pread` per day request, same pattern as the docstore.

**Coverage on day 1.** PRD-021 has been capturing event paragraphs
since ~2026-05-13. So the timeline shows roughly 12 days of dense
data on launch. Acceptable for a soft launch; would be embarrassing
as a default homepage. The soft-launch path lets the index fatten
naturally before we promote.

### 3. Entity colour system

Each "recurring entity" gets a stable colour. Recurring means: at
least 3 events on at least 2 distinct days in the past 60 days.

Colour derivation:

- **Hash the docno** to a fixed-pallete index. We control the
  palette (16 colours; chosen for distinguishability and WCAG-AA
  contrast against both white text and the muted-grey card body)
  rather than letting hashing pick arbitrary colours that may clash
  or be unreadable.
- **Persist the assignment.** A small JSON file
  `events.entity_colors.json` maps `docno -> palette_index`.
  Assignments are stable until manually edited. New entities pick
  the next palette slot that has the fewest assignees.
- **Non-recurring entities** render in a default neutral grey. Only
  the visually-memorable entities get colour, which keeps the
  signal strong.

The palette ships in `index.html` as CSS custom properties:
`--ent-1` through `--ent-16`. The JSON map is rendered into the page
as a small inline JSON blob, looked up by docno when each card
renders.

This system is what makes the timeline feel different from "yet
another news site." When the user sees the same red across three
different days separated by a week, the timeline is doing
something newspapers cannot.

### 4. Card layout: the day row

A day is rendered as:

- **Day label** in the left gutter (date number large, day-of-week
  + month small below).
- **A timeline dot** marking the day on the central spine.
- **A horizontal row of event cards** (responsive: 3-4 columns on
  desktop, 1-2 on mobile).
- Cards sorted by `rank_hint` desc.

Each card:

- Background colour = entity colour (or neutral grey).
- Image at top (Wikipedia article image, with PRD-027-style
  top-anchored crop for portraits).
- Entity tag pill in the top-left, semi-transparent.
- Title (the article title) in card-emphasis weight.
- 1-2 line excerpt of the `event_paragraph`.
- Tap → expands inline to show the full paragraph + a "Read article"
  link to `/?q=<entity>` (which routes to our existing search).

### 5. Rank hint: which events are big

We need a per-event importance score so:

- The masonry / brick layout (later zoom level) knows which tiles
  to make large.
- The day-row card order is sensible (Iran's IAEA framework is more
  newsworthy than a Premier League title settled by draw).

`rank_hint` proxies:

- **Spike score** from `current.json` if the entity is in today's
  trending — directly available.
- **Number of distinct Wikipedia paragraphs citing the event date**
  — proxy for "how many articles think this matters." Computed
  during the offline index build.
- **Entity class weight** — place + event slightly heavier than
  work + human, to bias against celebrity gossip. Tunable.

Combined as a simple weighted sum. Documented in the build script;
expected to need tuning iterations.

### 6. Zoom levels (v1.5)

v1 ships the **week zoom level** only — the day-row layout above,
showing the past 7 days, scrolling lazily to older days.

v1.5 adds:

- **Day zoom** — a single day, with the masonry brick layout from
  variant C in the mockups, plus a single-event detail panel on
  click.
- **Month zoom** — a calendar-style grid with each day showing its
  top 2-3 entity colours. Hover a day to preview, click to drill
  in.
- **Year zoom** — the year strip from variant D. Stacked
  entity-density bars by month. This is the killer "Iran every
  month" visualisation and the strongest argument for the whole
  product — but it needs ~6 months of data to look good, so it
  cannot be the launch story.

URL state: `/news`, `/news/2026-05-25`, `/news/2026-W21`,
`/news/2026-05`, `/news/2026`. Browser back/forward navigate
across zoom levels.

Mouse-wheel zoom is *not* in v1 — we use explicit "zoom out"
links per day-row instead. Wheel hijacking has bad ergonomics on
trackpads and we want to avoid that fight in v1.

### 7. Search toolbar

Sticky top of page; small (40px height); same logo on the left,
input field, ⌘K hint on the right. Focus-on-keypress (any letter
or `/` while not in an input field). Same suggest dropdown as the
existing homepage. Submission routes to `/?q=...` — the existing
search results page is unchanged.

The bet: search remains one keystroke away. Users who land on
`/news` and want to search do not feel locked into the timeline.

### 8. "Why does this article say that?" affordance

Each event card has a small `source` indicator showing which
Wikipedia article paragraph the event was extracted from. Tap →
expand the full paragraph in-line, with a "View on Wikipedia" link
to the article's diff history for the paragraph. This is the
*authority chain* — we are not making news, we are surfacing what
Wikipedia editors wrote.

Important framing: this is the trust differentiator. A user
hovering "why does this card say that?" should see, in two clicks,
the exact Wikipedia paragraph and the contributor history. No
other news app offers this.

(For news-RSS-sourced events (PRD-022 fallback path), the source
indicator shows the news outlet and links to the headline. We are
transparent about the source even when it is not Wikipedia.)

---

## Why this works

- **Differentiation.** Nobody else is doing "Wikipedia-grounded news
  timeline." Google News surfaces publishers. Wikipedia's own ITN
  is a sidebar. We are filling a niche of one.
- **Substrate already exists.** PRD-021/022/025/026 have done most
  of the data work. This PRD is mostly presentation.
- **Defensible.** Wikipedia is licensed CC-BY-SA. Anyone could
  build this, but the work compounds: our trending samples, our
  entity-classification graph, our event-paragraph extraction are
  ours. A clone would have to redo all of it.
- **Brand-safe.** "The news, as Wikipedia tells it" is a phrase
  with positive associations. Even users who disagree with
  Wikipedia's framing trust the *process* visibility.
- **Composable with our other surfaces.** A click on a timeline
  card routes to existing search. The knowledge panel + related
  rail experience users already get is intact.
- **Mobile-natural.** Vertical timeline is the right gesture for
  phones. The PRD-028 iOS app gets a story-shaped home view for
  free if we expose the events index via `/api/events` (which we
  should, alongside this).

---

## Risks

- **Sparse data on launch.** ~12 days of event paragraphs at PRD
  draft time. The timeline will look thin for the first 30-60 days.
  The soft-launch on `/news` lets us fatten the index naturally
  before promoting. Backfill is a separate, optional pipeline
  (Open question 1).
- **Iran-fatigue.** If the same handful of entities dominate every
  day, the colour system becomes the colour-monotony system. We
  may need a "top-N entities per day capped at 1 each" rule to
  promote variety. Visible in v1; tunable.
- **Editorial framing of Wikipedia framing.** Showing "Iran agrees
  to IAEA inspections" without nuance is itself a framing choice.
  We mitigate by:
    - linking the source paragraph in two clicks,
    - using the exact paragraph text Wikipedia editors wrote (no
      AI rewrites for v1; PRD-021's news summaries are out of scope
      here because they re-summarise),
    - showing the `captured_at` timestamp so users see the lag.
- **Promotion regret.** If we promote `/news` to `/` and bounce
  rate spikes, search-engine users may not come back. Mitigation:
  promotion is a one-line nginx-equivalent (Caddy redirect) toggle
  and rolling it back is symmetric.
- **Sparse-on-mobile.** A single-column timeline on a small phone
  with only 12 days of data looks like ~20 cards total. Padding
  the visual density may need an "interesting events from last
  year" filler section, which is out of v1 scope. Soft-launch
  reveals whether this matters.

---

## Pipeline

Offline:

```
trending/history.jsonl  (already captured by PRD-021)
    │
    ▼
tools/build_events_index.py  (new, daily timer)
    │
    ▼
events.jsonl + events.idx + events.entity_colors.json
    │
    ▼
server.py loads events.idx at startup; reads events.jsonl on demand
    │
    ▼
GET /news (server-rendered HTML for initial paint, JS for zoom)
GET /api/events?from=YYYY-MM-DD&to=YYYY-MM-DD (JSON; for the iOS
  app under PRD-028 to consume the same data)
```

Builder behaviour:

- **Daily** systemd timer at 04:00 UTC (after Wikimedia's pageview
  dumps usually settle).
- **Idempotent**: skips events already in the output.
- **Append-only**: never deletes; corrections come from re-running
  with a `--reindex` flag that does a clean rebuild.

Server-side:

- `events.idx` is a packed `{date_iso: byte_offset}` dict, ~10 KB/yr.
- `events.jsonl` is read with `os.pread` for a date range; ~50-200
  events per day × 200-400 bytes = ~20-80 KB per day-read.
- All reads happen behind FastAPI's async layer; under the same
  in-process model as other endpoints.
- `/api/events` is rate-limited (same global limiter as `/search`)
  to prevent crawlers from scraping the entire index in one
  request.

---

## Soft-launch path

1. **Week 1-2**: build the index + `/news` route + day-row layout
   only. Soft-launch on `/news` with no link from `/`. Word-of-mouth
   surface only.
2. **Week 3**: add a small "Today" pill on `/` that links to
   `/news`. Measure click-through.
3. **Week 4**: if click-through is healthy and `/news` return-visit
   rate exceeds `/`, add zoom levels (day, month).
4. **Week 5-6**: year strip + entity-colour consistency at full
   scale.
5. **Week 8 or later**: decide on `/` promotion. Use a feature
   flag so we can roll back.

Each step is reversible. The PRD does not commit to promoting
`/news` to `/`; it commits to *building the option*.

---

## Success metrics

Three signals, in priority order:

1. **Return-visit rate within 7 days.** Today's homepage probably
   has a low single-digit return rate (search-engine pattern). The
   timeline target: ≥ 20% of `/news` visitors return within 7 days.
   This is the "is this a reason to come back?" test.
2. **Time on page.** Search pages turn over in seconds; timeline
   pages should support multi-minute browsing. Median ≥ 2 minutes.
3. **Search-from-toolbar rate.** Users who land on `/news` and
   then run a search are using us as both a destination and a
   search engine. Target: ≥ 15% of `/news` sessions include a
   search.

Anti-metric: if the addition of `/news` reduces search activity on
`/`, we are cannibalising rather than expanding. Watch for it.

---

## Build estimate

Working solo, with no parallel work, assuming ordinary debugging:

| Stage | Time |
|---|---|
| `tools/build_events_index.py` (read history.jsonl, dedupe, join images, emit indexed file) | 1-2 days |
| `events.entity_colors.json` builder + palette + persistence | 0.5 day |
| Server: `/news` route, server-rendered initial paint, lazy-load JSON for later days | 1-2 days |
| Server: `/api/events` JSON endpoint | 0.5 day |
| Frontend: vertical timeline, day rows, card layout, entity colours, source-paragraph reveal | 2-3 days |
| Frontend: sticky search toolbar with focus-on-keypress | 0.5 day |
| Mobile responsive pass | 1 day |
| Daily systemd timer for the index build | 0.5 day |
| Polish, copy, "as Wikipedia tells it" framing throughout | 1 day |
| **Total** | **~8-11 days** |

This is too big for an experimental fork that we might abandon. We
should treat it as a branch we ship behind a feature flag (`/news`
route hidden unless `?preview=1`), then evaluate after week 1-2.

---

## Open questions

1. **Backfill?** PRD-021 has 12 days of event paragraphs. To make
   the year strip work, we want ~6-12 months. A backfill pipeline
   would walk historical Wikipedia revisions for top entities and
   extract dated paragraphs from past versions. This is a multi-day
   build on top of PRD-029 v1, with real Wikipedia-API rate-limit
   risks. Defer to v2 unless v1 measurably needs it.

2. **Entity-grouping for the colour system.** Iran (the country),
   Tehran (the city), and Iran's_supreme_leader are three docnos
   but probably want one colour. PRD-025's entity-class graph
   helps here — same-class entities at high relatedness could
   collapse to a single colour group. v1 ships per-docno; v1.5
   adds grouping.

3. **What about events Wikipedia editors haven't documented yet?**
   PRD-022 already fills this gap with Google News RSS fallback,
   and those events flow through to the timeline tagged
   `source: "news_rss"`. The question is how to render those
   distinctly — they are not "according to Wikipedia," they are
   "according to Google News, awaiting Wikipedia." Options:
   render at lower visual weight; or omit entirely. Lean toward
   "render with a different source label" so the timeline does not
   misrepresent itself.

4. **Calendar mechanics.** Day boundaries are UTC for us
   internally; users live in local time zones. Showing a US user
   their "yesterday" when our pipeline thinks it is two days ago
   is jarring. Probably solve by rendering in browser-local time
   but caching events keyed by UTC date. Worth thinking about
   before launch but not architecturally hard.

5. **What replaces the chip rail on `/`?** While `/news` is in
   soft-launch, `/` keeps its chip rail. After promotion (if it
   happens), the chip rail is redundant. Probably becomes the
   "today" section of the timeline page header and the homepage
   reduces to a clean search box. Decide at promotion time, not
   now.

6. **Is "Z" the right brand mark for a news site?** The current
   homepage shows the search box prominently and the wordmark is
   subtle. A news timeline benefits from a stronger masthead.
   Probably a small wordmark redesign at promotion time. Out of
   v1 scope.

---

## Why now

- We have the data substrate accidentally (PRD-021/022/025/026).
  Most of the offline work is already happening for trending; the
  marginal cost is the index builder and the route.
- iOS app (PRD-028) is being built and will benefit from a
  `/api/events` endpoint regardless of whether the web timeline
  ships. So at least the data work has two consumers.
- Search-engine-shaped products are commoditised. The product is
  not improved by being "Wikipedia search but slightly better than
  the last one"; it is improved by being a different shape.
- The brand position is unclaimed today. In six months somebody
  else may stake it. Wikipedia could ship an in-news redesign and
  cut us off; equally a third party could build it on top of CC-BY
  Wikipedia data without our trending pipeline. The trending
  pipeline is the moat for ~6 months.

---

## Decision asked

Approve the soft-launch path:

1. Build PRD-029 v1 (day-row timeline at `/news`) behind a feature
   flag, on a branch. Estimated ~8-11 days.
2. Run for 2-4 weeks, measure return-visit and time-on-page.
3. Decide on promotion based on data, with explicit rollback path.

The v2 zoom levels and backfill pipeline are not approved by this
PRD; they are scoped here so we can decide what to build next based
on what v1 tells us.
