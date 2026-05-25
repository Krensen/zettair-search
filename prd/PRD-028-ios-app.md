# PRD-028: iOS App — Native Search Client with System-Level Integration

**Status:** Draft
**Author:** metabot
**Date:** 2026-05-19

---

## Problem

zettair.io works well as a website. It does not earn a place on a home
screen. Visiting it requires opening a browser, navigating to a URL,
typing into the search box — the same friction as any other website.

iOS provides system-level affordances that a browser cannot reach:
Spotlight search suggestions, Siri shortcuts, home-screen widgets,
share-extensions, lock-screen widgets, offline storage, haptics,
universal links. An app that ships these features inserts itself into
the moments of curiosity that currently route through Google or
Wikipedia's own app.

A pure WKWebView wrapper would not earn those affordances. A native
client built against our existing JSON API can, and the API is already
well-shaped for it: `/search`, `/suggest`, `/api/trending`,
`/api/related`, `/click` are public, stateless, and return clean JSON.

The bet behind this PRD: the difference between "a Wikipedia search
website" and "the way I search Wikipedia" is iOS-specific surface area
the website can never have.

---

## Goal

Ship a native SwiftUI iOS app that:

1. Reproduces the website's core flows (search, knowledge panel,
   trending, related entities, cite-this) using the existing JSON API.
2. Adds iOS-only surfaces the website cannot provide: Spotlight/Siri
   integration, home-screen widget, share extension, offline cache of
   recently-viewed articles, universal links, native context menus and
   haptics, reading list with iCloud sync.

The app is a client of the existing backend. No accounts, no
server-side per-user state. The same Hetzner box serves both web and
iOS traffic; the only backend addition is a single `/article` endpoint
for the offline cache.

The app ships in two visible cuts: v1 (paths A–E below, ~5 weeks of
work) and v1.1 (paths F–G, polish, +~3–5 weeks). Submission to the App
Store happens between the two.

---

## Non-goals

- **Android app.** Different toolchain, different system affordances,
  different review process. If this works on iOS, Android is a separate
  PRD.
- **A standalone backend for the app.** No mobile-specific backend
  service. The app uses the same `server.py` the website does.
- **User accounts / login / sync of search history.** Reading list
  syncs via iCloud (Apple-managed); search history stays device-local.
- **Push notifications.** No real signal to push on. "Mark Carney is
  trending" is annoying.
- **Apple Watch app.** Almost nobody searches on a watch.
- **iPad-redesigned layout.** Make it work in `Size class .regular`
  (split view, three-column NavigationSplitView) but don't ship an
  iPad-specific UI in v1.
- **Audio mode / TTS.** PRD-023 idea, but iOS's system "Speak Screen"
  covers this already.
- **In-app browser for clicked Wikipedia links.** Use `SFSafariViewController`
  (free, system-managed, respects user's Safari settings).
- **Monetisation.** Free, no ads, no in-app purchases in v1.

---

## High-level architecture

```
iOS App (Swift / SwiftUI, iOS 16+ target)
  │
  ├─ App target — main UI, search, results, knowledge panel, trending
  │    ↓ HTTPS
  │   zettair.io  → Caddy → server.py
  │    /search, /suggest, /api/trending, /api/related, /article (new), /click
  │
  ├─ Widget Extension — home-screen + lock-screen widgets reading /api/trending
  │
  ├─ Share Extension — receives selected text from other apps, hands off via App Group
  │
  ├─ Intents Extension — AppIntents for Spotlight / Siri / Shortcuts
  │
  └─ Shared Framework — networking, models, CoreSpotlight indexing,
      offline cache (SQLite or Core Data), reading list (CloudKit)
```

Backend additions (`zettair-search`):

- `/article?docno=...` — returns cleaned article body text from the
  existing `_docstore`. Used by the offline cache. ~2 hours of work;
  the data is already on disk.
- Distinct `User-Agent` filtering in `digest.py` so iOS traffic is
  separable in metrics.
- `apple-app-site-association` JSON served at
  `https://zettair.io/.well-known/apple-app-site-association` for
  Universal Links. Caddy static-files it; no Python changes.

No other backend changes in v1.

---

## Pieces

### A. Spotlight & Siri integration (system-level search) — *highest leverage*

The killer feature. Users type into iOS Spotlight (swipe down on home
screen); the app's suggestions appear inline, ranked alongside system
results. Tap one → straight into the app at that result.

Three sub-features:

1. **`CoreSpotlight`** — index frequently-searched queries (top
   ~5000 from `autosuggest.json`, fetched on app launch) and every
   article the user has viewed as `CSSearchableItem`s. iOS's Spotlight
   surfaces them automatically.

2. **`AppIntents` framework** (iOS 16+) — declare a
   `SearchZettairIntent` that takes a query string. Siri can run it
   ("Hey Siri, search Zettair for Mark Carney"). Shortcuts users can
   chain it into automations. Provides a parameter-aware Spotlight
   suggestion.

3. **`INSearchForItemsIntent` donations** — every in-app search
   donates an intent. iOS learns the pattern and starts proactively
   suggesting the app (lock-screen suggestion at typical use times,
   "Suggested Apps" in Spotlight).

Effort: ~1 week. Payoff: makes the app sticky beyond "another search
box."

### B. Home-screen widget

`WidgetKit` widget showing the current trending rail. Small / medium /
large sizes; lock-screen widget on iOS 16+.

- Timeline provider polls `/api/trending` every ~3 hours (matches the
  fetcher cadence).
- Small: 1 trending chip with thumbnail.
- Medium: 3–4 chips.
- Large: 6 chips with `event_paragraph` preview from PRD-021.
- Lock-screen widget (rectangular): single trending headline.

Tap → opens the app at a prefilled search.

Effort: 3–5 days. Cost: trivial on battery/network — `WidgetKit`
schedules updates conservatively and the system can throttle.

### C. Share extension

User selects text in any app (Safari, Messages, Notes) → Share →
"Search Zettair". Drops the user into the main app with the query
prefilled. This is the feature that inserts the app into the moment of
curiosity rather than waiting to be opened.

- Separate `Share Extension` target.
- Reads selected text via `NSExtensionItem` → `kUTTypePlainText`.
- Hands off to the main app via an `App Group` `UserDefaults` write
  plus a custom URL scheme open.

Effort: 2–3 days. Mostly Xcode plumbing.

### D. Offline cache for recently-viewed articles

When the user taps a result, the article text is already in our
docstore. Fetch full text + image, store locally. Last ~50–100 viewed
articles are then readable in airplane mode (subway, plane, dead
signal).

- Backend change: `/article?docno=...` endpoint returning the cleaned
  docstore body. The data is already there; this is a thin reader
  around `_docstore.pread()`.
- iOS side: SQLite (`GRDB` or raw `SQLite.swift`) for storage. Core
  Data is overkill for what's effectively a key/value blob store.
- Storage budget: cleaned text averages ~5–30 KB per article. 100
  articles ≈ 2 MB. LRU eviction at 100 entries or 10 MB.
- UI: cached badge on previously-viewed results; "Offline" indicator
  in the status area when network is unreachable; transparent
  hot-cache on network restore.

Effort: backend endpoint ~2 hours; iOS ~3–4 days for storage + UI
states.

### E. Universal Links

`https://zettair.io/search?q=einstein` and
`https://zettair.io/?q=einstein` open directly in the app if installed,
fall back to website otherwise. Standard iOS feature.

- `apple-app-site-association` JSON at the well-known path on
  zettair.io (Caddy static-serves; no Python).
- `Associated Domains` capability in Xcode.
- URL handler in `App` scene to parse `?q=...` and route to the search
  screen.

Effort: half a day. Cheap and high-impact — every shared link opens
natively.

### F. Reading list & saved searches

Local-only product surface beyond search. User stars an article or
saves a query; both surface in a "Saved" tab. Synced via iCloud so it
persists across devices and reinstalls.

- `NSUbiquitousKeyValueStore` for the small case (≤1 MB total). If
  reading list grows, migrate to `CloudKit` private database.
- Tab structure: Search / Saved / History / Settings.
- Saved query rerun pulls the latest results — it's the query that's
  saved, not the results.
- Saved article links to the offline-cached body if available, or
  fetches fresh.

Effort: 4–5 days. Genuine product surface beyond "search box."

### G. Context menus, haptics, native gestures

The small stuff that makes the app feel native instead of webview-y:

- `UIImpactFeedbackGenerator` on chip tap, result tap, citation copy,
  star-to-save.
- Long-press a result → context menu: "Open in Safari", "Share", "Cite
  this", "Add to reading list", "Copy link".
- SwiftUI `.swipeActions` on results: right-swipe to save, left-swipe
  to share.
- Pull-to-refresh on trending.
- Drag-and-drop a result (especially for iPad split view).
- System dark mode, Dynamic Type, VoiceOver labels throughout.

Effort: dribbles across the project; ~1 week if taken seriously. This
is also where iPad split-view layout lands (NavigationSplitView with
sidebar / results / detail columns).

### H. Backend changes (one endpoint, one config file)

1. **`GET /article?docno=...`** — returns cleaned article body text
   from the existing docstore. JSON body: `{docno, title, body, url,
   image}`. Already in `_docstore`; this is a thin reader.

2. **`apple-app-site-association`** — static JSON at
   `https://zettair.io/.well-known/apple-app-site-association`. Caddy
   serves it; no Python change. Lists the app's bundle ID and the
   path patterns that should deep-link (`/search`, `/?q=*`).

3. **User-Agent split in `digest.py`** — distinguish web vs iOS
   traffic in metrics. The app sets `User-Agent:
   ZettairIOS/<version>`. One-line filter change.

4. **CORS / rate-limit confirmation** — `/search`, `/suggest`,
   `/api/trending`, `/api/related` are already public and the app uses
   the same paths. No new policies. Confirm `Caddy` doesn't strip
   anything per-route.

No new database, no new service, no new build step in `setup.sh`. The
backend is incidentally a mobile backend because it's already a clean
JSON API.

### I. Things explicitly skipped

- **Push notifications.** No signal. Skip.
- **Apple Watch app.** Skip.
- **TTS / audio mode.** System covers it.
- **Login / accounts / cross-device search history.** iCloud handles
  the reading list; search history stays device-local for privacy.
- **In-app paywall, tipping, IAP.** Skip.
- **Live Activities** (Dynamic Island). Cute but no event-shaped
  workload to host there.

---

## App structure (SwiftUI)

```
@main App
  └─ RootView
        ├─ SearchTab
        │    ├─ SearchBar (autosuggest via /suggest)
        │    ├─ TrendingRail (/api/trending, mirrors website rail)
        │    ├─ ResultsList (/search)
        │    │    ├─ KnowledgePanelCard (when summary present)
        │    │    ├─ ResultRow × N
        │    │    │    ├─ thumbnail, title, snippet, URL
        │    │    │    ├─ context menu: save/share/cite/open
        │    │    │    └─ tap → ArticleDetail (SFSafariViewController OR offline cache)
        │    │    └─ RelatedEntitiesPanel (right rail on iPad, bottom rail on phone)
        │    └─ CiteThisSheet (PRD-024 popover, native sheet)
        ├─ SavedTab
        │    ├─ SavedQueries
        │    ├─ SavedArticles (offline-readable)
        │    └─ History (device-local)
        └─ SettingsTab
              ├─ Cache size + clear button
              ├─ Default citation format
              ├─ Toggle: Spotlight indexing on/off
              └─ About / version / link to website

Widget Extension
  └─ TrendingWidget (small / medium / large / lockscreen)

Share Extension
  └─ ShareViewController — captures selected text → opens main app

Intents Extension
  └─ SearchZettairIntent — AppIntent for Siri / Shortcuts
```

Minimum iOS version: **iOS 16**. AppIntents (the modern Siri /
Shortcuts API) and lock-screen widgets both require it. iOS 16 is the
sensible floor as of 2026.

---

## Milestones

### M1 — Project scaffolding + networking (~3 days)

- Xcode project with App / Widget / Share / Intents targets.
- Bundle ID, App Group, signing.
- `ZettairAPI` Swift package: typed clients for `/search`, `/suggest`,
  `/api/trending`, `/api/related`, `/click`, `/article`.
- Codable models for search results, knowledge panel, trending items,
  related entities.
- Skeleton SwiftUI app with three tabs and placeholder views.

### M2 — Search results UI (~1 week)

- SearchBar with autosuggest dropdown.
- ResultsList with native row layout matching the website's
  information density.
- KnowledgePanelCard with markdown rendering
  (`swift-markdown-ui`).
- Result tap → `SFSafariViewController` for Wikipedia article.
- Click logging to `/click`.

### M3 — Trending + Related entities (~3 days)

- TrendingRail loading from `/api/trending`, with in-index vs
  external-link distinction (PRD-020 design).
- RelatedEntitiesPanel below results (phone) or right rail (iPad).
- Pull-to-refresh.

### M4 — Cite this + iOS sheets (~2 days)

- Native sheet (`.sheet`) reproducing the cite-this popover
  (PRD-024) — APA / MLA / Chicago / Harvard / BibTeX.
- Long-press on a result invokes the cite sheet.
- `UIPasteboard` copy with haptic confirmation.

### M5 — Spotlight + AppIntents + Siri (~1 week)

- `SearchZettairIntent` declared in Intents extension.
- `CoreSpotlight` indexes top-5000 queries from autosuggest at first
  launch.
- Every in-app search donates an intent.
- Manual test: query appears in Spotlight; "Hey Siri, search Zettair
  for X" works; Shortcuts can chain the intent.

### M6 — Backend `/article` endpoint + offline cache (~4 days)

- Add `GET /article?docno=...` to `server.py`. Reads from
  `_docstore`; mirrors the existing enrich path but returns full
  body. ~50 lines.
- iOS side: SQLite cache; "Save for offline" action; LRU eviction;
  offline indicator; transparent hot-cache.

### M7 — Home-screen widget (~4 days)

- Trending widget (small / medium / large).
- Timeline provider with 3-hour refresh.
- Tap-through opens app with prefilled query (intent handling).
- Lock-screen widget (rectangular family).

### M8 — Share extension (~3 days)

- Receives selected text from any app.
- Hands off via App Group + URL scheme.
- Main app opens prefilled to that query.

### M9 — Universal Links (~half day)

- `apple-app-site-association` deployed to Caddy.
- `Associated Domains` capability in Xcode.
- URL handler parses `?q=...`.

**(End of v1. ~5 weeks of net work. Submit to TestFlight here.)**

### M10 — Reading list + iCloud sync (~5 days)

- SavedTab with Saved Queries / Saved Articles / History.
- iCloud sync via `NSUbiquitousKeyValueStore` (or CloudKit if size
  forces).
- Persistence across device reinstall.

### M11 — Context menus, haptics, swipe actions, iPad layout (~1 week)

- All polish features from section G.
- iPad NavigationSplitView with sidebar / results / detail columns.

### M12 — App Store prep (~1 week)

- App icon (1024×1024 + all sizes).
- Launch screen.
- Screenshots for 6.7", 6.1", and iPad Pro 12.9" sizes (3+ each).
- Privacy nutrition label (no data collected from user beyond
  device-local search history).
- App Store description, keywords, promotional text.
- TestFlight beta with a handful of users.
- Submission and inevitable rejection-cycle.

### M13 (deferred) — Notifications, Live Activities, Apple Watch

If genuine signals or workloads emerge after launch. Currently nothing
to push.

### M14 (deferred) — Android

Separate PRD.

---

## Calendar estimate

Assuming Swift / SwiftUI fluency:

| Weeks | Phase |
|-------|-------|
| 1 | Scaffold + networking + results UI |
| 2 | Knowledge panel + trending + related entities + cite-this |
| 3 | Spotlight + AppIntents + Siri donations |
| 4 | Widget + share extension + universal links |
| 5 | Offline cache (incl. `/article` backend endpoint) |
| 6 | Reading list + iCloud sync |
| 7 | Context menus, haptics, swipe actions, iPad layout |
| 8 | Polish: animations, empty states, error states, accessibility |
| 9 | App Store: icon, screenshots, privacy nutrition label, TestFlight |
| 10 | Submission, rejection cycle, fixes, resubmit |

Net work: ~4–6 weeks. Calendar: ~8–10 weeks with App Store latency.

Add ~1–2 weeks of learning curve to any of this if not already fluent
in SwiftUI.

---

## Risks

- **App Store review rejection.** A search app is fine in principle,
  but reviewers can flag "Minimum Functionality" (Guideline 4.2) if
  the app feels like a wrapper. Path 3's iOS-only features (widget,
  Spotlight, share extension, offline cache) are exactly what 4.2
  asks for, so the design is review-defensive by construction.
  Mitigation: ship M5 + M7 + M8 before first submission, not after.

- **Wikipedia attribution.** CC BY-SA requires attribution on
  derivative content. Knowledge-panel summaries are derivative. The
  website handles this implicitly via the source link; the app needs
  explicit footer attribution on every summary and a "Read full
  article on Wikipedia" link on every result. Standard practice; not
  hard, but easy to forget.

- **API load from app traffic.** Per-install load with the widget
  refreshing every 3h, Spotlight donations, and proactive search is
  higher than a website session. Mitigation: existing rate limiting
  in Caddy; offline cache reduces repeat-article load; widget polling
  is conservative by `WidgetKit` design. If the app grows past
  ~10k installs, revisit (separate caching layer or a CDN in front
  of `/api/trending`).

- **Backend coupling.** The app becomes a public API consumer of
  endpoints that were previously implementation details (URL shapes,
  JSON field names). Mitigation: version the API via a path prefix
  for the iOS client (`/v1/search` etc.) before submission, or
  promise field-stability and never rename in place. Lean toward the
  latter; the URLs are already stable.

- **Maintenance burden doubles.** Every backend change needs to
  consider the iOS client. Universal links and `/article` become
  part of the public contract. Mitigation: keep the API surface
  small; the app uses the same six endpoints that already exist
  plus one new one.

- **iOS-version churn.** AppIntents is iOS 16+. iOS 17 added
  interactive widgets; iOS 18+ may shift again. Mitigation: target
  iOS 16, opt into newer features behind `#available` checks,
  don't chase the bleeding edge.

- **CCX13 saturation.** Loadtest shows ~18 req/s with current
  features; widget refresh fan-out (every install pings every 3h)
  could push baseline up. Mitigation: `/api/trending` is mtime-cached
  in `server.py` and trivially cacheable in Caddy with a 5-minute TTL
  for app traffic. Set that before launch.

- **TestFlight friction for solo dev.** First TestFlight build often
  takes 2–3 days for internal testing review. Plan around it.

- **Solo Apple Developer account ($99/year).** Renewal lapses kill
  the app entirely; calendar a reminder.

- **No analytics in v1.** Privacy nutrition is cleaner without them,
  but it means no real visibility into what users do. Mitigation:
  the existing `/click` and `/search` logs cover top-line metrics
  (queries, clicks, distinguish app via User-Agent). Skip
  per-screen analytics for v1.

---

## Open questions

- **Login / cross-device search history.** Resolved: no. iCloud
  handles reading list; search history stays device-local.

- **Bundle ID and App Store name.** "Zettair" is already the project
  name; "Zettair Search" as the App Store display name is the obvious
  choice. Bundle ID: `io.zettair.app` or similar — pick before M1
  since it's hard to change later.

- **App icon design.** Reuse the website favicon scaled up, or design
  a fresh icon? Probably the latter; favicons rarely scale to 1024².
  Outsource to a designer for a few hundred dollars or commission a
  small mark.

- **Free vs paid.** Free in v1. Tipping or "support the developer"
  IAP could come later if there's any usage signal worth monetising.

- **iPad vs iPhone scope.** Build iPad as a stretch of iPhone (`Size
  class .regular`) in v1; consider a true iPad redesign only after
  iPhone ships. Don't let iPad slow down the launch.

- **Versioning the API.** Path-prefix (`/v1/search`) vs
  field-stability promise. Lean toward stability — the API is mature
  enough that breaking changes are unlikely, and a prefix forces
  parallel code paths on the server forever.

- **Open-source the iOS app?** The website is in a public repo
  already; consistent with that to publish the iOS source. Probably
  yes, in a third repo `Krensen/zettair-ios`, MIT-licensed.

- **Submission timing.** Submit at end of M9 (v1) or wait through
  M11 (full polish)? Probably M9 — TestFlight gets useful feedback,
  and the v1.1 update can ship within weeks of v1 with the rest.

- **Spotlight indexing size.** 5,000 queries from autosuggest is the
  initial target. Could go to 50k. Storage is cheap; the question is
  whether more entries dilute relevance. M5 tune.

- **Widget refresh cadence vs `/api/trending` cadence.** The fetcher
  runs every 3h; widget polls every 3h. Aligning them means at most
  one stale-ish refresh per install per cycle. Fine.

- **`/article` endpoint vs reusing `/search`.** Could fetch the
  article via `/search?q=<title>&n=1` and read the snippet, but the
  snippet isn't the full body. A dedicated `/article` endpoint that
  reads from `_docstore` is cleaner and necessary for offline
  reading.

---

## Server-side handoff (2026-05-24)

Coordination log between the iOS effort and the backend repo. Updated
as asks land or get closed.

### Closed

- **/img proxy returning identical bytes regardless of requested width.**
  Fixed in zettair-search 2026-05-24. Root cause was the proxy's
  thumbnail-rewrite path: any /{N}px-/ token was being unconditionally
  rewritten to /250px-/. Now only non-allowlisted widths are rewritten,
  and to the nearest larger allowed width. Whitelisted widths
  (20/40/60/120/250/330/500/960/1280/1920/3840) pass through unchanged.
  Also: the proxy's blanket `except Exception: return 404` now
  distinguishes upstream HTTPError (pass code through), URLError
  (502), and other errors (502).

- **image_url on /api/trending.** Added 2026-05-24. Each chip entry
  now carries `image_url` looked up from `_images_store` against the
  item's docno. Same null semantics as result-row `image_url`
  (absent when no image). Saves the iOS home view N parallel
  /search?n=1 calls per render.

### Open (in the zettair repo, not this one)

- **Default thumb width in the offline image extractor.** Currently
  `enwiki_top1m_images.store` writes URLs with /300px-/. 300 is not on
  Wikimedia's allowlist, so every fetch through /img triggers a
  rewrite. Changing the default to /500px-/ in
  `zettair/wikipedia/<image-builder>.py` (path tbc) would:
    - cover result-row thumbs (64pt @ 3x = 192px) and KP thumbs
      (90pt @ 3x = 270px) natively;
    - leave the brief hero (~1100px) as the only case still
      benefiting from a /img rewrite up to 960px;
    - require no API change — iOS clients with the existing
      rewrite logic stay compatible.
  Lands with the next corpus refresh per setup.sh; no immediate
  redeploy needed. Server-side rewrite stays as a safety net for
  older sidecar data and for the higher-res hero path.
