# PRD-023: Feature Ideas Backlog — Differentiators vs Wikipedia Search

**Status:** Idea backlog (not yet prioritised for build)
**Author:** metabot
**Date:** 2026-05-13

---

## Why this doc exists

Wikipedia's built-in search is a list of article titles. We already
have two features Wikipedia doesn't and probably wouldn't build:

- A **per-query knowledge panel** with a summary (PRD-018, PRD-021,
  PRD-022 + the Mac Mini offline pipeline).
- A **trending chip rail** with news context (PRD-020 + PRD-021).

This document captures ideas for the next set of differentiating
features. Each is a candidate for its own PRD once we pick what to
build next. Ranked roughly by impact vs effort, but the ranking is
subjective — the goal is to not lose ideas, not to prescribe order.

---

## Foundations already in place

When evaluating these ideas, remember what we already have on prod:

- **Mac Mini summariser pipeline** — offline LLM compute with a
  priority queue, generation prompts, FlatStore storage, atomic
  rsync round-trip. Reusable for any "small model writes some prose"
  feature.
- **Click logs** (PRD-006) — every result-click logged with query +
  rank + score. Mined for the click prior; could feed personalisation
  or "related but different" ideas.
- **BM25F retrieval** with per-field weighting (PRD-019). Title,
  caption, infobox, category, see-also fields all weighted
  independently. Strong baseline for surfacing the right doc.
- **FlatStore pattern** — concatenated binary + JSON offset map,
  `os.pread()` per access. Used for snippets, images, summaries,
  URLs. Pattern is reusable for any per-doc precomputed asset.
- **Image proxy + image store** (PRD-021 image-proxy fixes). Every
  doc has a lead image cached.
- **Trending pipeline** with `current.json` + `recently_seen.json`
  + news cache. Daily / hourly signal of what's hot.
- **Periodic rebuild** of the index from a clickstream-driven cut
  (PRD-012). Newest data is roughly monthly.

---

## The infrastructure constraint (read this before evaluating any idea)

We have **one Mac Mini doing batch LLM work** via a queue-based
pipeline. Each generation takes 30s-4min. Sustained throughput is
maybe 50-200 generations per day. That divides every idea below
cleanly into two buckets:

**Batch-friendly (✓ works as-is):** anything precomputable per-article
or per-fixed-set, that can be ground through over hours or days.
Frontend-only features are also in this bucket (no LLM needed).

**Live-inference-required (✗ doesn't fit):** anything where the user's
input is unique each time and we can't precompute the response.
Live LLM calls would mean either: (a) building a synchronous endpoint
the Mac Mini can't sustain at any meaningful load, or (b) paying for
hosted inference (Groq/Together/etc) which adds cost + a third-party
dependency.

The categorisation isn't a veto on live-inference ideas — but it tells
us which ideas need a different infrastructure conversation before we
can build them. A "cache the head, fall through the tail" hybrid is
sometimes the answer; sometimes it's "wait until we have a budget for
hosted inference".

Per-idea feasibility flags appear below as **Infra: ✓ / ✗ / hybrid**.

---

## Ideas

### 1. AI-powered "ask my question" search

**Infra: hybrid (head cache) / ✗ for long tail without hosted inference.**
The most differentiated idea here is also the one our infrastructure
struggles most with. Every question is unique, so a Mac Mini batch
pipeline can't sustain live answering. Two paths: (a) pre-generate
answers for the top N likely questions in batch and serve them from
a FlatStore, falling back to plain search for the long tail; (b)
add a hosted-inference provider (Groq, Together, OpenAI) for the
live path, paying per-query. Worth doing as a separate PRD that
picks a path explicitly.

A second search mode (or a tab next to the regular results) where the
user types a natural-language question and gets a 2-3 paragraph
answer synthesised from the top 3-5 matching articles, with inline
citations.

**Mechanism.** Question → BM25F search → top 5 article docs → LLM
prompt: "Answer this question using ONLY the following sources;
inline-cite each fact". Reuse the existing Mac Mini path for offline
batch generation, or run synchronously for popular questions via a
hot cache.

**Why it works.** Perplexity / ChatGPT have shown that
question-answering UX is what readers actually want — they don't
want a list of links, they want the answer. Ours would be
Wikipedia-grounded, free, no signup. Lower hallucination risk than
unbounded LLMs because the retrieval scope is tight.

**Build estimate.** 1-2 weeks. New endpoint, prompt design,
citation parsing, frontend tab. Cache layer matters; cold synthesis
is too slow for live.

**Risks.** Hallucination is real even when grounded. Need a
parse-time check that cited spans actually exist in the cited
source. Latency: needs caching for popular questions.

---

### 2. Compare view — side-by-side article comparison

**Infra: hybrid.** The two side-by-side panels are pure retrieval +
existing summaries — no new LLM work. The "key differences" panel
needs an LLM call per pair, which doesn't scale to arbitrary query
pairs at live latency. Realistic path: precompute the differences
panel for the top ~1000 popular query pairs in batch (one Mac Mini
run gets that done overnight) and serve from cache; for un-cached
pairs, show the two panels without the third panel (or show "compare
notes loading…" and generate offline for next time).

Query like `python vs ruby` or `bordeaux vs burgundy` renders two
panels side by side: lead paragraphs, infobox-extracted facts, common
categories. Third panel synthesised by the Mac Mini: "Key
differences".

**Mechanism.** Detect "X vs Y" pattern in the query (or a UI affordance
"Compare with…"). Two BM25F searches, two top-1 hits. Lay out the
two articles' summaries side by side. Third call to the summariser:
"Summarise the key differences between A and B given their lead
paragraphs and infobox data."

**Why it works.** Comparison is one of the highest-intent searches
on the web. Google does shallow "X vs Y" panels but nothing in
Wikipedia's category. Genuinely differentiating; visually striking;
demos beautifully.

**Build estimate.** ~1 week. Query parser + UI layout + new
prompt. Reuses everything else.

**Risks.** Some "X vs Y" queries are about non-comparable things
("python vs anaconda" — the snake or the distro?). Need a sanity
check before committing to the comparison layout.

---

### 3. Timeline view for events / people

**Infra: ✓ batch.** Per-article precomputation; the Mac Mini grinds
through them once. Updates only when an article changes
significantly. Output stored in a FlatStore alongside the existing
summary stores.

For event or biography queries, extract dated content from the
article body and render as a vertical timeline (date + one-sentence
event description). Wikipedia articles bury dates in walls of prose;
a timeline is faster.

**Mechanism.** Reuse PRD-021's `find_event_paragraph` date-extraction
regex but generalise: extract every dated sentence, not just the
most recent. Cluster nearby dates, summarise each cluster to a short
event description (Mac Mini). Render as a vertical timeline.

**Why it works.** Wikipedia's strength is historical depth but the
prose format hides it. We already have the date-extraction
infrastructure. Visualisation is a real value-add.

**Build estimate.** 1 week. Heavier on the offline path because
every biography / event article needs date extraction + per-cluster
summarisation.

**Risks.** Quality depends on how cleanly dates parse out of prose.
Some articles structure their content with dates already (good);
some intersperse dates with year-only references (bad).

---

### 4. Reading time + difficulty signal

**Infra: ✓ batch (no LLM).** Pure computation — word count and a
readability metric. One-time pass at index build, stored in a
sidecar.

Each result shows "5 min read · accessible" or "23 min read ·
technical". Computed offline.

**Mechanism.** At index time, compute per-article word count
(reading time = words / 250 wpm) and Flesch-Kincaid grade level
(accessible / moderate / technical thresholds). Store in a sidecar
or in the docmap. Frontend renders the two pills under each result.

**Why it works.** Students and researchers especially want to know
what they're committing to. Wikipedia gives you no idea. Cheap signal,
real value.

**Build estimate.** 1-2 days. One-time computation pass over the
docstore; sidecar like the field-lengths sidecar from PRD-019.
Frontend additions are minimal.

**Risks.** None really. Worst case the signal is wrong on a few
articles; not user-breaking.

---

### 5. "Cite this" button on every result

**Infra: ✓ frontend-only.** No backend, no LLM. Pure client-side
formatting of fields the result already carries.

Hover a result → "Cite" → instant APA / MLA / Chicago / BibTeX
formatted citation copied to clipboard. Or click → modal with a
copy button per format.

**Mechanism.** Pure frontend. The result already carries title +
URL; we add a `cite` action that formats four citation styles
using current date for "retrieved". No backend changes.

**Why it works.** Citation generators are a $$ market (EasyBib, Cite
This For Me). Adding it is half a day. Students return for this
alone. Wikipedia has a "Cite this article" link buried in the
sidebar; we'd put it front-and-centre on every result.

**Build estimate.** Half a day. All frontend.

**Risks.** None. Could even pre-populate from BibTeX standards
without per-article tuning.

---

### 6. Saved searches / reading lists

**Infra: ✓ frontend-only initially.** localStorage for v1; if we
later add accounts for sync that's a tiny user-data endpoint, no
LLM.

Anonymous reading lists stored in localStorage initially. Click a
"Save" affordance on any result → it joins your sidebar list.
Optionally upgrade to accounts for cross-device sync later.

**Mechanism.** localStorage list of `{docno, title, saved_at}`.
Frontend renders a sidebar drawer. Shareable via URL hash if we
encode the docno list. Backend can stay unaware of this for the
v1.

**Why it works.** Returns are the retention problem. Wikipedia
watchlists are for editors, not readers. This builds re-visit
behaviour with zero infrastructure cost initially.

**Build estimate.** 2-3 days. All frontend until we want sync,
then add a tiny user-data endpoint.

**Risks.** localStorage limits, but generous (5-10MB per origin
typically — plenty for thousands of saved items). Privacy
considerations if we later add accounts.

---

### 7. "Related but different" suggestions

**Infra: ✓ batch (no LLM).** Pure offline aggregation over
clicks.jsonl, recomputed nightly. Result is a JSON map served from
disk.

Below results: "Readers also searched for…" derived from our click
logs. Different from Wikipedia's "See also" (editor-curated):
ours is real reader behaviour.

**Mechanism.** Offline aggregation over `clicks.jsonl`: for each
query, find queries that other readers searched immediately before
or after. Surface top 3-5 as a chip rail below the results.
Cache in a JSON map.

**Why it works.** We have click data. Wikipedia has click data but
doesn't surface it in search. Showing reader paths is fascinating
behaviour-as-content. Builds session length naturally.

**Build estimate.** 3-4 days. Offline aggregation + a small API
endpoint + frontend chip rendering. Recomputation cadence: nightly.

**Risks.** Privacy considerations — needs the aggregations to be
non-identifying (k-anonymity threshold). Low risk at our volume but
worth designing in.

---

### 8. Article quality heuristic

**Infra: ✓ batch (no LLM).** Extracted from the Wikipedia API at
index time, stored in the docmap or a sidecar.

Each result tagged with a small badge: "Featured", "Good article",
"Stub", "Contested" (NPOV disputes, neutrality tags), based on
Wikipedia's quality flags.

**Mechanism.** Wikipedia exposes these flags via `prop=info` in
the API or via categories like "Featured articles". Extract at
index time, store in docmap or sidecar. Frontend renders.

**Why it works.** Quality varies wildly on Wikipedia. Surfacing
the article's quality status is information Wikipedia already
has but doesn't expose in search UI. Cheap; instantly useful.

**Build estimate.** 1-2 days. Extraction at index time, frontend
badge.

**Risks.** Quality flags are noisy — older "Featured" articles
might have degraded. Worth labelling with the date the flag was
applied if we can extract it.

---

### 9. Daily / weekly digest email

**Infra: ✓ batch (no NEW LLM).** Reuses the news summaries we
already generate for the homepage. The daily cron just renders +
sends. Cost is the email service (Mailgun/Postmark/SES), not LLM.

User enters email → daily email with the day's trending articles +
their news panels. No accounts needed initially; just email +
unsubscribe.

**Mechanism.** New `subscriptions.json` table. Daily cron renders
the trending list + news panels into an HTML email, sends via
Mailgun / Postmark / SES. Unsubscribe via signed URL.

**Why it works.** Email re-engagement is the most reliable
retention channel for non-app products. The trending pipeline is
the input; we just need rendering + send.

**Build estimate.** 1-2 weeks. Email service signup, HTML template,
cron job, unsubscribe handling, GDPR / CAN-SPAM compliance.

**Risks.** Spam-folder risk if we don't warm a sending domain.
Deliverability requires actual care. Costs scale with subscriber
count.

---

### 10. Image-rich result mode

**Infra: ✓ frontend-only.** The image store is already on disk;
we just render differently.

Toggle: "Show results as image grid" — Pinterest-style layout,
each tile is the article's lead image + title. For
exploration queries (`art nouveau`, `medieval castles`, `octopus
species`) it's far better than a text list.

**Mechanism.** Frontend-only toggle. The image store already has
the lead images via PRD-018's image_url field. Render a CSS grid
instead of the list when toggle is on. Lazy-load images.

**Why it works.** Wikipedia is image-rich but search results show
zero images. We have the infrastructure. Visually striking and
demos well.

**Build estimate.** 1-2 days. All frontend; CSS grid + lazy
loading + the toggle persisted in localStorage.

**Risks.** Articles without images need a placeholder. Some image
URLs are stale (PRD-022 image-proxy issue). Handle via the
existing onerror = hide pattern.

---

### 11. Time-machine / "as of date" search

**Infra: ✓ batch (no LLM).** This is a storage and indexing
problem, not a generation problem. Mac Mini batch fits fine —
snapshots are pulled on a schedule, the build is offline. The
cost is disk, not compute.

Query box has a date picker. Search "donald trump" as of
`2019-06-01` and you get the article *as it was* on that date,
plus snippets / summary from the historical revision.

**Mechanism.** Periodically snapshot the lead paragraphs of each
article (or the full text, more storage) and index those by
docno + date. Search hits the historical FlatStore for the
selected date.

**Why it works.** Powerful for journalists, historians,
researchers. Genuinely unique — no one does this. Wikipedia has
revision history but no way to search against historical state.

**Build estimate.** 3-4 weeks. Snapshot pipeline, historical
docstore, search-time date routing, frontend date picker. The
storage is the hard part — 1.5M articles × N snapshot dates × ~5
KB of leads = 10s of GB for a few years of history.

**Risks.** Storage and rebuild cost grow over time. Could start
with a small window (last 12 months) and expand.

---

### 12. Voice / audio mode

**Infra: ✓ if browser TTS / ✗ if hosted TTS.** Web Speech API is
free and runs in the user's browser — zero server load. Quality
is "fine, not great" and varies by platform. If we want
higher-quality voices (ElevenLabs / OpenAI TTS) we'd pre-render
audio for popular articles in batch on the Mac Mini and store .mp3
in a FlatStore; long tail falls back to Web Speech. Same
hybrid pattern as the AI ask-a-question idea.

Click a "Listen" button on a result → article read aloud via TTS,
starting with the lead paragraph and continuing on demand.

**Mechanism.** Browser TTS (free, Web Speech API) for v1. Falls
back to ElevenLabs / OpenAI TTS for nicer voice if user has the
network for it. Pause/play/skip controls.

**Why it works.** Commuting / cooking / driving use case. Niche
but loyal audience. Accessibility is a real win too.

**Build estimate.** 2-3 days. Frontend Web Speech wiring is easy;
nicer voice via API takes longer if we want to pre-render audio
for popular articles.

**Risks.** Web Speech voices are variable across browsers /
platforms. Quality is "fine" not "great".

---

## My read on prioritisation (with infra reality folded in)

The original "ship the AI ask-a-question feature next" hits the
Mac-Mini-only-and-batch wall hard — that one needs a hosted-inference
budget OR a head-query cache PRD before it's buildable.

Revised top 3, all of which fit the current infra:

1. **#5 Cite this** — half a day, frontend-only, captures student
   market, no infra impact.
2. **#10 Image grid mode** — 2 days, frontend-only, uses the image
   store we already have, demos beautifully.
3. **#4 Reading time + difficulty** — 1-2 days, batch precompute
   (no LLM), quietly useful on every result.

These three all ship within a week and all use what we have.

After those, the highest-impact batch-friendly features are:

- **#7 Related but different** — uses our click data, distinctive
  signal Wikipedia doesn't surface.
- **#11 Time-machine search** — heavy storage but pure batch
  pipeline.
- **#3 Timeline view** — per-article precompute, reuses the
  date-extraction infrastructure from PRD-021.

The "headline differentiators" that need an infra conversation:

- **#1 AI ask-a-question** — needs either a hosted-inference budget
  or a head-query batch cache. Write a separate PRD that picks
  a path before building.
- **#12 Voice/audio (hosted TTS variant)** — same pattern; the
  Web Speech browser variant is fine on current infra.
- **#2 Compare view (key-differences panel)** — the precomputed
  popular-pairs path is batch-friendly; arbitrary pairs aren't.

The ranking is a starting point, not a decision. Pick what excites
you — but if it's a ✗ or hybrid idea, the first deliverable should
be the infra-decision PRD, not the user-facing feature.

---

## Ideas explicitly deferred / out of scope

- **Personalisation** (user-specific re-ranking). Requires sign-in,
  privacy considerations, A/B infrastructure. Big feature; revisit
  after we have stable returns from one of the simpler ideas.
- **Mobile app**. Better mobile-web should come first.
- **Multilingual**. en.wikipedia is the target for now; everything
  else is post-product-market-fit.
- **Replacing Wikipedia as the source**. The differentiation is
  better UX on the same content, not different content.
