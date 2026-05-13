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

## Ideas

### 1. AI-powered "ask my question" search

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

## My read on prioritisation

If we picked three to build next, in this order:

1. **#5 Cite this** — half a day, captures student market, no risk.
2. **#10 Image grid mode** — 2 days, instantly distinctive in demos,
   uses existing infrastructure.
3. **#1 AI-powered ask my question** — the big WOW feature, 1-2
   weeks, addresses the search intent Wikipedia can't.

That sequence gives a quick win, a demo-able win, and then the
headline differentiator. Each builds confidence in the next.

But the ranking is a starting point, not a decision. The user
should pick what excites them.

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
