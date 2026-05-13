#!/usr/bin/env python3
"""
fetch_trending.py — PRD-020 trending-pages fetcher.

One run:
  1. Download the most recent complete hourly pageview dump from
     dumps.wikimedia.org.
  2. Filter to en.wikipedia, filter junk via the denylist, normalise
     titles into search queries.
  3. Append the filtered top-1000 to history.jsonl.
  4. Recompute current.json:
       - mode = "spike" if we have >= MIN_SAMPLES_FOR_SPIKE samples for
         enough articles to fill the rail, else "raw".
       - spike score = log( (now + smoothing) / (median(history) + smoothing) )
       - In spike mode, drop items below SPIKE_THRESHOLD; fall back to
         topping up with raw rank if too few qualify.

Atomic writes — current.json is written via .tmp + rename so readers
(server.py) never see a partial file.

Invocation:
    python3 fetch_trending.py            # one fetch + write
    python3 fetch_trending.py --compact  # trim history > 30 days

Exit codes:
    0  success (or 'nothing new yet' — wikimedia hasn't published the
       expected hour, common at the schedule boundary).
    1  hard failure (network unrecoverable, parser broken, etc.).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# -- config -----------------------------------------------------------------

TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR",
    "/mnt/wikipedia-source/trending",
))
HISTORY_PATH = TRENDING_DIR / "history.jsonl"
CURRENT_PATH = TRENDING_DIR / "current.json"
LOG_PATH     = TRENDING_DIR / "fetch.log"

DENYLIST_PATH = Path(os.environ.get(
    "ZET_TRENDING_DENYLIST",
    str(Path(__file__).parent / "trending_denylist.txt"),
))

# Spike scoring constants. See PRD-020 for derivation.
# SMOOTHING bumped 100 -> 500 after early prod data was dominated by
# long-tail sleeper articles (Tamil politicians, regional places).
# With smoothing 100 a 50-views article jumping to 5000 scored ~3.5,
# beating a 10000->30000 spike on a genuinely popular article (~1.1).
# 500 compresses that gap.
SMOOTHING               = 500
# Minimum current views to qualify. En.wikipedia hourly traffic is
# in the millions; below ~1000/hour is mostly long-tail noise from
# non-English readers, communities briefly piling on, or botnets.
# This is a coarse floor; the real bot/spike-shape filter lives
# below (see "shape filter").
MIN_VIEWS_NOW           = 1000
# Minimum median baseline. An article that's normally invisible
# (<50/hour median) doesn't deserve to be on the homepage even if it
# spiked — too easy to game, too noisy.
MIN_MEDIAN_BASELINE     = 50
HISTORY_WINDOW_DAYS     = 30
MIN_SAMPLES_FOR_SPIKE   = 21    # ~7 days at 3-hourly cadence
SPIKE_THRESHOLD         = math.log(2.0)   # >= 2x median to qualify
RAIL_MIN                = 4     # if < this many spike, fall back to raw
RAIL_MAX                = 12    # PRD-021: aim for 6-12 chips after specificity gate

# Shape filter — the previous sample must already be at least this
# ratio above baseline. Real trending articles ramp over multiple
# hours; bot pile-ons and community-of-the-hour effects show as a
# single isolated spike. 1.15 is looser than the initial 1.3 — still
# rejects "one hour and gone" bursts (where prev is at baseline,
# ratio ~1.0) but lets early-ramp stories through where the previous
# sample is only modestly elevated.
SHAPE_PREV_MIN_RATIO    = 1.15

# -- PRD-021: article-specificity gate --------------------------------------
# Each pageview-shape-qualifying candidate is enriched by fetching its
# Wikipedia article and finding the highest-scoring "recent dated event"
# paragraph. Candidates without a qualifying paragraph fall off the rail.
WIKI_API_URL            = "https://en.wikipedia.org/w/api.php"
# Bumped 50 -> 150 alongside TOP_SAMPLE_KEEP 3000 -> 10000. ~150
# Wikipedia API calls per fetch = ~75s of work; well within the 3-hour
# cycle. Without this bump the wider candidate pool would be wasted
# because the gate only fans out to the top 50.
MAX_CANDIDATES_TO_GATE  = 150
MIN_PARA_LEN            = 120   # chars; below this is too short to be a real event para
# Loosened after first prod run: with 56h of bootstrap history and a
# narrow candidate pool, the strict gate (score>=4, fresh<=14d) found
# zero matches across all 7 spikers and the rail went empty. We accept
# a few more false positives in exchange for a non-empty rail. Will
# tighten back once the history window naturally widens.
MIN_SPECIFICITY_SCORE   = 2     # was 4 — allows month-precision dates alone to qualify
EVENT_FRESHNESS_DAYS    = 30    # was 14 — admits month-old events
EVENT_PARA_MAX_CHARS    = 2000  # truncate event_paragraph stored in current.json
# Widened 1000 -> 3000 originally, then 3000 -> 10000 to give the
# specificity gate more filter surface. News stories at rank 3000-10000
# in the global hourly pageview list still pass the spike-shape filter
# regularly; widening here catches them.
TOP_SAMPLE_KEEP         = 10000

# -- PRD-022: news-headline fallback ---------------------------------------
# When the Wikipedia paragraph gate misses, fetch Google News RSS for the
# query and synthesise an event_paragraph from the top headlines. Same
# downstream pipeline as Wikipedia-sourced events.
NEWS_RSS_URL_TEMPLATE   = (
    "https://news.google.com/rss/search?"
    "q={q}&hl=en-US&gl=US&ceid=US:en"
)
NEWS_FETCH_TIMEOUT_S    = 8
NEWS_CACHE_HOURS        = int(os.environ.get("ZET_NEWS_CACHE_HOURS", "3"))
NEWS_FRESHNESS_DAYS     = 14
NEWS_MAX_HEADLINES      = 5
NEWS_MIN_HEADLINES      = 2     # below this we don't fall back at all
NEWS_CACHE_DIR          = TRENDING_DIR / "news_cache"
# Query qualifier — empirically "+ wikipedia" over-filtered (returned 0
# headlines for queries with strong news, including Andy Burnham and
# Taylor Swift). Disabled by default; can be re-enabled if we see
# wrong-entity drift in practice.
NEWS_QUERY_QUALIFIER    = ""

# -- PRD-026: quality filters -----------------------------------------------
# Applied to the union of all candidate sources (spike + Google News
# top-stories + Wikipedia ITN). Drops noise before items reach the rail.
QUALITY_HEADLINE_MAX_AGE_DAYS = 7   # top Google News headline must be this fresh
QUALITY_STALE_OBIT_DAYS       = 30  # dead-person + obit-headlines + older than this -> drop

# Mainstream news outlets — at least one of the top-3 headlines must
# come from one of these for a candidate to pass the quality filter.
# Hand-curated; biased toward English-language outlets but covers most
# major regions. Tunable list — easy to expand if we see legit news
# being filtered.
MAINSTREAM_SOURCES_RE = re.compile(
    r"\b(bbc|reuters|new york times|nytimes|the times|the guardian|"
    r"washington post|associated press|\bap\b|bloomberg|the independent|"
    r"financial times|\bft\b|the hindu|the economist|al jazeera|cnn|"
    r"npr|abc news|cbs news|nbc news|sky news|the telegraph|"
    r"the times of india|the wall street journal|wsj|the atlantic|"
    r"politico|axios|propublica|the conversation|the wire|"
    r"bloomberg news|cnbc|forbes|fortune|reuters world|"
    r"the new yorker|economic times|hindustan times|le monde|"
    r"der spiegel|deutsche welle|france 24|abc \(australia\)|"
    r"the sydney morning herald|the age|stuff|nz herald)\b",
    re.I,
)

# Marketing-pattern phrases that indicate a self-promo / recap / clickbait
# headline rather than real news. Extended set after PRD-026 analysis.
MARKETING_RE = re.compile(
    r"\b(trailer|teaser|poster|release date|cast announced|first look|"
    r"premieres|opening weekend|box office|behind the scenes|featurette|"
    r"special presentation|streaming now|now streaming|new episode|"
    r"season \d+ episode|episode \d+|easter eggs|ending explained|"
    r"how to watch|where to watch|free download|streaming guide|"
    r"watch online|in theaters|in cinemas|now playing|"
    r"binge guide|recap|review:)\b",
    re.I,
)

# Obituary-flavoured headlines. Used as part of the stale-obituary check.
OBIT_RE = re.compile(
    r"\b(died|death|passed away|obituary|remembered|tribute|"
    r"anniversary of (his|her|their) death|posthumous|"
    r"in memoriam|memorial)\b",
    re.I,
)

# Where to cache per-docno "is in deaths-YYYY category" lookups (24h TTL).
DEATH_CACHE_PATH = TRENDING_DIR / "death_cache.json"
DEATH_CACHE_TTL_HOURS = 24

# -- PRD-026: Google News top stories ---------------------------------------
GOOGLE_TOP_URL          = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
GOOGLE_TOP_CACHE_PATH   = TRENDING_DIR / "google_top_cache.json"
GOOGLE_TOP_CACHE_HOURS  = 3        # match the trending cadence
GOOGLE_TOP_MAX_ITEMS    = 50       # how many to keep per fetch
ENTITY_TITLES_PATH      = Path(os.environ.get(
    "ZET_ENTITY_TITLES",
    "/mnt/wikipedia-source/related/entity_titles.tsv",
))

# -- PRD-026: Wikipedia In-the-news -----------------------------------------
WIKI_ITN_URL_TEMPLATE   = (
    "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/"
    "{year}/{month:02d}/{day:02d}"
)
WIKI_ITN_CACHE_PATH     = TRENDING_DIR / "wiki_itn_cache.json"
WIKI_ITN_CACHE_HOURS    = 24       # portal updates daily

# Cap on the final rail size after the quality filter.
# This already lives further up as RAIL_MAX; keeping the reference here
# to make it explicit which constant bounds the output.

# Dump URL template. {Y}/{Y-M}/pageviews-{YMD}-{HH}0000.gz
DUMP_URL_TEMPLATE = (
    "https://dumps.wikimedia.org/other/pageviews/"
    "{year}/{year}-{month:02d}/pageviews-{year}{month:02d}{day:02d}-{hour:02d}0000.gz"
)
USER_AGENT = "zettair-search/PRD-020 (zettair.io; hugh@viaaltoadvisors.com)"

# Project filter — wikipedia/desktop+mobile+app for en.
# In the hourly dumps, the project codes that count for "en wikipedia,
# all access" are: en, en.m. (We exclude en.zero and en.wikibooks etc.)
EN_PROJECTS = {"en", "en.m"}


# -- structural denylist (regex) --------------------------------------------

# These match the URL-form title (with underscores). We apply these BEFORE
# normalisation so we don't have to special-case the rules.
_STRUCTURAL_PATTERNS = [
    re.compile(p) for p in [
        r"^Main_Page$",
        r"^-$",                       # placeholder for "no referrer" dumps
        r"^Special[:_].*",
        r"^Wikipedia[:_].*",
        r"^File[:_].*",
        r"^Portal[:_].*",
        r"^Help[:_].*",
        r"^Category[:_].*",
        r"^Template[:_].*",
        r"^User[:_].*",
        r"^Talk[:_].*",
        r".*_talk[:_].*",
        r"^\d{4}$",                  # bare year
        r"^\d{4}_in_.*",
        r".*_in_\d{4}$",
        r"^Deaths_in_\d{4}.*",
        r"^Births_in_\d{4}.*",
        r"^List_of_.*",
        r"^Lists_of_.*",
        r"^Index_of_.*",
        r"^Glossary_of_.*",
        r"^Outline_of_.*",
        r"^Timeline_of_.*",
    ]
]


def load_user_denylist() -> set[str]:
    """Lowercased substrings from the editable denylist file."""
    if not DENYLIST_PATH.exists():
        return set()
    out = set()
    for line in DENYLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.lower())
    return out


def is_denied(title: str, user_denyset: set[str]) -> bool:
    for pat in _STRUCTURAL_PATTERNS:
        if pat.match(title):
            return True
    lower = title.lower().replace("_", " ")
    for needle in user_denyset:
        if needle in lower:
            return True
    return False


# -- logging ----------------------------------------------------------------

def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# -- title normalisation ----------------------------------------------------

_DISAMB_PAREN = re.compile(r"_\([^()]+\)$")
_TRAILING_PUNCT = re.compile(r"[\.,!?]+$")


def title_to_query(title: str) -> str:
    """Wikipedia URL-form title -> human search query.

    Mercury_(planet) -> mercury
    Apple_Inc.       -> apple inc
    2026_Australian_federal_election -> 2026 australian federal election
    """
    t = title
    t = _DISAMB_PAREN.sub("", t)
    t = t.replace("_", " ")
    t = urllib.parse.unquote(t)
    t = _TRAILING_PUNCT.sub("", t)
    t = " ".join(t.split())
    return t.lower()


def title_to_display(title: str) -> str:
    """URL-form title -> human-readable display, preserving case.

    Mercury_(planet) -> Mercury
    """
    t = _DISAMB_PAREN.sub("", title)
    t = t.replace("_", " ")
    t = urllib.parse.unquote(t)
    return t


# -- dump fetch + parse -----------------------------------------------------

def latest_complete_hour() -> dt.datetime:
    """Return the hour we'll attempt first. Wikimedia's publishing lag
    is empirically 2-3 hours from the end of the hour; we aim for an
    hour that ended ~2.5h ago. If that dump 404s, the caller walks
    backwards through earlier hours."""
    now = dt.datetime.now(dt.UTC)
    target = (now - dt.timedelta(minutes=150)).replace(minute=0, second=0, microsecond=0)
    return target


def fetch_latest_available(max_lookback_hours: int = 12) -> tuple[dt.datetime, dict[str, int]] | None:
    """Try the latest hour; on 404 walk back hour-by-hour until we find
    a published dump. Returns (hour, counts) or None if nothing in the
    last `max_lookback_hours` is available — extremely unlikely unless
    dumps.wikimedia.org is down."""
    start = latest_complete_hour()
    for h in range(max_lookback_hours):
        hour = start - dt.timedelta(hours=h)
        if already_have(hour):
            log(f"already have sample for {hour.isoformat()}, walking further back")
            continue
        counts = fetch_dump(hour)
        if counts:
            return hour, counts
    return None


def fetch_dump(hour: dt.datetime) -> dict[str, int]:
    """Fetch one hourly pageview dump and return {title: views} for en
    (desktop + mobile combined). Returns {} if the dump 404s."""
    url = DUMP_URL_TEMPLATE.format(
        year=hour.year, month=hour.month, day=hour.day, hour=hour.hour,
    )
    log(f"fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            buf = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"  dump 404 — not yet published")
            return {}
        raise
    log(f"  got {len(buf):,} bytes")

    counts: dict[str, int] = {}
    with gzip.open(io.BytesIO(buf), "rt", encoding="utf-8", errors="replace") as gz:
        for line in gz:
            # Format: <project> <title> <views> <bytes>
            # Split on the first 3 whitespace runs only; titles may
            # contain unusual characters but never whitespace.
            parts = line.rstrip("\n").split(" ", 3)
            if len(parts) < 3:
                continue
            project, title, views_s = parts[0], parts[1], parts[2]
            if project not in EN_PROJECTS:
                continue
            try:
                v = int(views_s)
            except ValueError:
                continue
            counts[title] = counts.get(title, 0) + v
    log(f"  parsed {len(counts):,} en titles")
    return counts


# -- history append ---------------------------------------------------------

def append_history(hour: dt.datetime, top_titles: list[tuple[str, int]]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "t": hour.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": top_titles,
    }
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def already_have(hour: dt.datetime) -> bool:
    """Idempotency: return True if history.jsonl already has a record
    for this hour. The timer can fire multiple times if the previous
    run was slow; we don't want duplicate samples."""
    if not HISTORY_PATH.exists():
        return False
    target = hour.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Cheap reverse scan — recent samples are at the tail.
    with open(HISTORY_PATH, "rb") as f:
        # Read last 64 KB; samples are tiny so this covers many days.
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 65536))
        tail = f.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("t") == target:
            return True
    return False


def read_history(limit_days: int = HISTORY_WINDOW_DAYS) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=limit_days)
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                t = dt.datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            except (KeyError, ValueError):
                continue
            if t < cutoff:
                continue
            out.append(rec)
    return out


# -- scoring ----------------------------------------------------------------

def compute_current(history: list[dict], user_denyset: set[str]) -> dict:
    """Read all of history, score articles, return the current.json
    payload (not written here — caller writes it atomically)."""
    if not history:
        return {
            "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sample_window": None,
            "mode": "raw",
            "items": [],
        }

    history = sorted(history, key=lambda r: r["t"])
    latest = history[-1]
    earlier = history[:-1]

    # Build per-article history of views (excluding the latest sample)
    series: dict[str, list[int]] = {}
    for rec in earlier:
        for title, views in rec["rows"]:
            series.setdefault(title, []).append(int(views))

    latest_views = {t: int(v) for t, v in latest["rows"]}

    # Decide mode: spike if >= RAIL_MIN articles in latest have enough history.
    qualifying = sum(
        1 for t in latest_views
        if len(series.get(t, [])) >= MIN_SAMPLES_FOR_SPIKE
    )
    mode = "spike" if qualifying >= RAIL_MIN else "raw"

    # Score and project items.
    items: list[dict] = []
    seen_query: dict[str, dict] = {}  # dedupe by normalised query

    for rank, (title, views) in enumerate(latest["rows"], 1):
        if is_denied(title, user_denyset):
            continue
        query = title_to_query(title)
        if not query:
            continue

        hist = series.get(title, [])
        median_baseline = statistics.median(hist) if hist else None

        if mode == "spike" and len(hist) >= MIN_SAMPLES_FOR_SPIKE:
            score = math.log((views + SMOOTHING) / (median_baseline + SMOOTHING))
        else:
            score = None   # not enough history to spike-score

        # In spike mode, apply qualifying floors. The spike threshold
        # alone lets long-tail sleepers (50 -> 5000 views) outrank
        # genuine giants growing (10000 -> 30000), so we also gate on
        # absolute views, minimum baseline, and curve shape.
        if mode == "spike":
            if score is None or score < SPIKE_THRESHOLD:
                continue
            if views < MIN_VIEWS_NOW:
                continue
            if median_baseline is None or median_baseline <= MIN_MEDIAN_BASELINE:
                continue
            # Shape filter: the most recent prior sample must already
            # be elevated. Bot bursts and community-of-the-hour effects
            # show as a single isolated spike (prev ≈ baseline). Real
            # trending articles ramp up over multiple hours so the
            # previous 3-hour sample is already above baseline.
            prev_views = hist[-1] if hist else 0
            prev_ratio = (prev_views + SMOOTHING) / (median_baseline + SMOOTHING)
            if prev_ratio < SHAPE_PREV_MIN_RATIO:
                continue

        item = {
            "query": query,
            "title": title_to_display(title),
            "docno": title,    # raw URL-form, used to join against the docmap server-side
            "rank": rank,
            "views": views,
            "median_baseline": median_baseline,
            "score": round(score, 3) if score is not None else None,
        }

        # Dedupe by query: keep the highest scoring (or highest views in raw).
        existing = seen_query.get(query)
        if existing is None:
            seen_query[query] = item
        else:
            keep_new = (
                (mode == "spike" and (item["score"] or 0) > (existing["score"] or 0))
                or (mode == "raw" and item["views"] > existing["views"])
            )
            if keep_new:
                seen_query[query] = item

    items = list(seen_query.values())

    if mode == "spike":
        items.sort(key=lambda r: r["score"], reverse=True)
        # No back-fill. The whole point of the floors + shape filter
        # is "only show real spikes". Padding the rail with raw
        # top-views articles (which are by definition NOT spikes)
        # would undo that. If too few articles qualify the rail is
        # short — the homepage hides it cleanly when empty.
    else:
        items.sort(key=lambda r: r["views"], reverse=True)

    items = items[:RAIL_MAX]

    sample_window = (
        f"{history[0]['t']}..{latest['t']}" if len(history) > 1
        else latest["t"]
    )
    return {
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sample_window": sample_window,
        "mode": mode,
        "items": items,
    }


def write_current(payload: dict) -> None:
    CURRENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURRENT_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=False)
    os.replace(tmp, CURRENT_PATH)


RECENTLY_SEEN_PATH = TRENDING_DIR / "recently_seen.json"


def update_recently_seen(payload: dict) -> None:
    """Maintain a {query_norm: last_seen_at} record of every query that
    has appeared on the spike rail. Older entries are pruned beyond
    SERVE_GRACE_DAYS so the file doesn't grow forever.

    Used by server.py to serve news summaries for a short grace
    window after a query drops off the rail — news doesn't flicker
    on every 3-hourly sample dip."""
    if payload.get("mode") != "spike":
        return
    SERVE_GRACE_DAYS = 7   # generous; serve-time check is the real gate
    seen: dict = {}
    if RECENTLY_SEEN_PATH.exists():
        try:
            with open(RECENTLY_SEEN_PATH, encoding="utf-8") as f:
                seen = json.load(f)
        except (OSError, json.JSONDecodeError):
            seen = {}
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in payload.get("items", []):
        q = (it.get("query") or "").strip().lower()
        if q:
            seen[q] = now_iso
    # Prune entries older than SERVE_GRACE_DAYS
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=SERVE_GRACE_DAYS)
    pruned = {}
    for q, ts in seen.items():
        try:
            t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if t >= cutoff:
            pruned[q] = ts
    tmp = RECENTLY_SEEN_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pruned, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, RECENTLY_SEEN_PATH)


# -- PRD-021: article-specificity gate --------------------------------------

# Date-format regexes. Day-precision scores 4, month 2, bare year 1.
# Validated on Tristan da Cunha (matches "9 May 2026") and Mark Carney
# (matches multiple "2025"/"2026"/"January 2026" dates) during PRD design.
_DAY_DM_RE  = re.compile(r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(202[0-9])\b")
_DAY_MD_RE  = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(202[0-9])\b")
_MONTH_RE   = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(202[0-9])\b")
_YEAR_RE    = re.compile(r"\b(202[0-9])\b")

_MONTH_TO_NUM = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}


def _strip_wikitext(s: str) -> str:
    """Quick-and-dirty wikitext -> plain text. Handles templates, refs,
    links, bold/italic, files/categories. Good enough for paragraph
    extraction; not aiming for perfect rendering."""
    # Strip templates (nested up to 3 levels)
    for _ in range(3):
        s = re.sub(r'\{\{[^{}]*\}\}', '', s)
    s = re.sub(r'<ref[^>]*>.*?</ref>', '', s, flags=re.S)
    s = re.sub(r'<ref[^>]*/>', '', s)
    s = re.sub(r'<!--.*?-->', '', s, flags=re.S)
    s = re.sub(r'\[\[(?:File|Image|Category):[^\]]*\]\]', '', s)
    s = re.sub(r'\[\[([^|\]]+)\|([^\]]+)\]\]', r'\2', s)
    s = re.sub(r'\[\[([^\]]+)\]\]', r'\1', s)
    s = re.sub(r"'''([^']+)'''", r'\1', s)
    s = re.sub(r"''([^']+)''", r'\1', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _latest_date_in(text: str) -> dt.date | None:
    """Find the most-recent date referenced in text, preferring more-
    precise formats. Day-precision dates take priority; if none, fall
    back to month-precision; if none, year-only. Returns None if no
    recognisable 202x date is present.

    The precision hierarchy prevents "9 May 2026" from being dragged
    forward to "2026-07-01" by a bare-year "2026" mention elsewhere in
    the same paragraph."""
    day_candidates: list[dt.date] = []
    for m in _DAY_DM_RE.finditer(text):
        day, month, year = int(m.group(1)), _MONTH_TO_NUM[m.group(2)], int(m.group(3))
        try:
            day_candidates.append(dt.date(year, month, day))
        except ValueError:
            pass
    for m in _DAY_MD_RE.finditer(text):
        month, day, year = _MONTH_TO_NUM[m.group(1)], int(m.group(2)), int(m.group(3))
        try:
            day_candidates.append(dt.date(year, month, day))
        except ValueError:
            pass
    if day_candidates:
        return max(day_candidates)
    month_candidates: list[dt.date] = []
    for m in _MONTH_RE.finditer(text):
        month, year = _MONTH_TO_NUM[m.group(1)], int(m.group(2))
        try:
            month_candidates.append(dt.date(year, month, 15))
        except ValueError:
            pass
    if month_candidates:
        return max(month_candidates)
    year_candidates: list[dt.date] = []
    for m in _YEAR_RE.finditer(text):
        try:
            year_candidates.append(dt.date(int(m.group(1)), 7, 1))
        except ValueError:
            pass
    return max(year_candidates) if year_candidates else None


def _specificity_score(text: str) -> int:
    """Score a paragraph by date-format precision. Day > month > year."""
    s = 0
    s += 4 * len(_DAY_DM_RE.findall(text))
    s += 4 * len(_DAY_MD_RE.findall(text))
    s += 2 * len(_MONTH_RE.findall(text))
    s += 1 * len(_YEAR_RE.findall(text))
    return s


def fetch_article_wikitext(docno: str) -> str | None:
    """Fetch the article's wikitext via the Wikipedia API. Returns None
    on any error (rate limit, missing article, network)."""
    params = {
        "action": "parse",
        "page": docno,
        "prop": "wikitext",
        "format": "json",
    }
    url = WIKI_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
    except (urllib.error.URLError, ValueError, json.JSONDecodeError):
        return None
    parse = d.get("parse") or {}
    wt = (parse.get("wikitext") or {}).get("*")
    return wt


def find_event_paragraph(wikitext: str, today: dt.date) -> dict | None:
    """Return the highest-scoring recent-event paragraph, or None.

    Returns dict with: paragraph (str), specificity (int), event_date (str ISO).
    """
    plain = _strip_wikitext(wikitext)
    best: tuple[int, str, dt.date] | None = None
    for p in plain.split("\n\n"):
        p = p.strip()
        if len(p) < MIN_PARA_LEN:
            continue
        if p.startswith(("=", "|", "*", "{", "}")):
            continue
        score = _specificity_score(p)
        if score < MIN_SPECIFICITY_SCORE:
            continue
        latest = _latest_date_in(p)
        if latest is None:
            continue
        age_days = (today - latest).days
        # Reject if the latest dated mention is older than the freshness
        # window or in the future. Future-dated paragraphs are usually
        # biographical mentions of scheduled events (election dates,
        # birthdays); we want events that have actually happened.
        if age_days > EVENT_FRESHNESS_DAYS or age_days < 0:
            continue
        if best is None or score > best[0]:
            best = (score, p, latest)
    if best is None:
        return None
    score, para, event_date = best
    if len(para) > EVENT_PARA_MAX_CHARS:
        para = para[:EVENT_PARA_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return {
        "paragraph": para,
        "specificity": score,
        "event_date": event_date.isoformat(),
    }


# -- PRD-022: Google News RSS fallback --------------------------------------

# RSS pubDate format: "Mon, 12 May 2026 14:30:00 GMT"
_RSS_DATE_RE = re.compile(
    r"^[A-Z][a-z]{2},\s+"           # "Mon, "
    r"(\d{1,2})\s+"                  # day
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{4})\s+"                    # year
    r"(\d{2}):(\d{2}):(\d{2})\s+"   # HH:MM:SS
    r"(GMT|UTC|[\+\-]\d{4})$"
)
_RSS_MONTH = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
# Google News title format is usually "Headline - Source" — extract both.
_GN_TITLE_RE = re.compile(r"^(.*?)\s+-\s+([^-]+)$")


def _parse_rss_pubdate(s: str) -> dt.datetime | None:
    if not s:
        return None
    m = _RSS_DATE_RE.match(s.strip())
    if not m:
        return None
    day, mon, year, hh, mm, ss, _tz = m.groups()
    try:
        return dt.datetime(int(year), _RSS_MONTH[mon], int(day),
                           int(hh), int(mm), int(ss), tzinfo=dt.UTC)
    except (ValueError, KeyError):
        return None


def _parse_news_rss(xml_bytes: bytes, today: dt.date, max_items: int | None = None) -> list[dict]:
    """Parse a Google News RSS response into a list of headline dicts.
    Filters out items older than NEWS_FRESHNESS_DAYS and in the future.
    Returns at most `max_items` (default NEWS_MAX_HEADLINES) newest first."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=NEWS_FRESHNESS_DAYS)
    items = []
    for item in root.iter("item"):
        title_raw = (item.findtext("title") or "").strip()
        pub_raw   = (item.findtext("pubDate") or "").strip()
        link      = (item.findtext("link") or "").strip()
        # Source comes from the <source> element when present. Google
        # News also appends " - Source" to the title; strip that to
        # keep the synthesised paragraph clean, regardless of whether
        # <source> was set.
        source = ""
        src_el = item.find("source")
        if src_el is not None and src_el.text:
            source = src_el.text.strip()
        title = title_raw
        m = _GN_TITLE_RE.match(title_raw)
        if m:
            title = m.group(1).strip()
            if not source:
                source = m.group(2).strip()
        pub = _parse_rss_pubdate(pub_raw)
        if pub is None:
            continue
        if pub < cutoff or pub.date() > today + dt.timedelta(days=1):
            continue
        items.append({
            "title": title,
            "source": source,
            "link": link,
            "pub_date": pub.isoformat(),
        })
    items.sort(key=lambda h: h["pub_date"], reverse=True)
    return items[:(max_items if max_items is not None else NEWS_MAX_HEADLINES)]


def fetch_news_headlines(query: str, today: dt.date | None = None) -> list[dict]:
    """Hit Google News RSS for `query` and return the top recent
    headlines (newest first). Returns [] on any failure — caller
    treats missing/empty identically."""
    if today is None:
        today = dt.datetime.now(dt.UTC).date()
    qstr = query + NEWS_QUERY_QUALIFIER
    url = NEWS_RSS_URL_TEMPLATE.format(q=urllib.parse.quote(qstr))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=NEWS_FETCH_TIMEOUT_S) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    return _parse_news_rss(data, today)


def fetch_news_headlines_cached(query: str, today: dt.date | None = None) -> list[dict]:
    """Disk-cached variant. Cache file lives in NEWS_CACHE_DIR keyed by
    a safe-form of the query. TTL = NEWS_CACHE_HOURS. We cache both
    successful hits and empty results — the rate of revisit per query
    matters more than perfect freshness on a transient miss."""
    if today is None:
        today = dt.datetime.now(dt.UTC).date()
    NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Make the query path-safe. query_norm is already lowercased with
    # collapsed whitespace; just replace anything filesystem-hostile.
    safe = re.sub(r"[^a-z0-9_-]+", "_", query.strip().lower())[:120] or "_empty_"
    cache_file = NEWS_CACHE_DIR / f"{safe}.json"
    now = dt.datetime.now(dt.UTC)
    if cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.strptime(
                payload["fetched_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.UTC)
            age_hrs = (now - fetched_at).total_seconds() / 3600.0
            if age_hrs <= NEWS_CACHE_HOURS:
                return payload.get("headlines", [])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass   # fall through to refetch
    headlines = fetch_news_headlines(query, today=today)
    try:
        tmp = cache_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "headlines": headlines,
            }, f, separators=(",", ":"))
        os.replace(tmp, cache_file)
    except OSError:
        pass   # cache write failure isn't fatal
    return headlines


def _prune_news_cache(max_age_days: int = 7) -> int:
    """Delete cache files older than max_age_days. Returns count
    deleted. Called once per fetch_trending run; cheap (directory
    listing + mtime check)."""
    if not NEWS_CACHE_DIR.exists():
        return 0
    cutoff = dt.datetime.now(dt.UTC).timestamp() - max_age_days * 86400
    n = 0
    for f in NEWS_CACHE_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n


def synthesise_news_paragraph(display_title: str, headlines: list[dict]) -> str | None:
    """Build an event_paragraph-equivalent from a list of Google News
    headlines. Returned string is shaped like a Wikipedia paragraph
    enough that the existing news-prompt path on the Mac Mini handles
    it correctly. Returns None if the headlines are too thin to be
    useful — caller treats as no event."""
    if len(headlines) < NEWS_MIN_HEADLINES:
        return None
    lines = [f"Recent news about {display_title}:"]
    for h in headlines:
        title = h.get("title", "").strip()
        source = h.get("source", "").strip()
        try:
            d = dt.datetime.fromisoformat(h["pub_date"]).strftime("%d %B %Y")
        except (KeyError, ValueError):
            d = ""
        if title and source and d:
            lines.append(f"- \"{title}\" ({source}, {d})")
        elif title and source:
            lines.append(f"- \"{title}\" ({source})")
        elif title:
            lines.append(f"- \"{title}\"")
    if len(lines) <= 1:
        return None
    return "\n".join(lines)


# -- PRD-026: death-category lookup (24h cached) ----------------------------

_DEATH_CACHE: dict | None = None


def _load_death_cache() -> dict:
    global _DEATH_CACHE
    if _DEATH_CACHE is not None:
        return _DEATH_CACHE
    if DEATH_CACHE_PATH.exists():
        try:
            _DEATH_CACHE = json.loads(DEATH_CACHE_PATH.read_text())
            return _DEATH_CACHE
        except (OSError, json.JSONDecodeError):
            pass
    _DEATH_CACHE = {}
    return _DEATH_CACHE


def _save_death_cache(cache: dict) -> None:
    try:
        DEATH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEATH_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, separators=(",", ":")))
        os.replace(tmp, DEATH_CACHE_PATH)
    except OSError:
        pass


def fetch_death_year(docno: str) -> int | None:
    """Returns the YYYY of the docno's 'YYYY deaths' category if any,
    or None. Cached on disk for DEATH_CACHE_TTL_HOURS."""
    cache = _load_death_cache()
    now = dt.datetime.now(dt.UTC)
    entry = cache.get(docno)
    if entry:
        try:
            fetched_at = dt.datetime.strptime(entry["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            if (now - fetched_at).total_seconds() / 3600 <= DEATH_CACHE_TTL_HOURS:
                return entry.get("death_year")
        except (KeyError, ValueError):
            pass
    url = ("https://en.wikipedia.org/w/api.php?action=query&prop=categories"
           f"&cllimit=30&titles={urllib.parse.quote(docno)}&format=json")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    death_year: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        for page in (d.get("query") or {}).get("pages", {}).values():
            for c in page.get("categories", []) or []:
                title = (c.get("title") or "").replace("Category:", "")
                m = re.match(r"^(\d{4}) deaths$", title)
                if m:
                    death_year = int(m.group(1))
                    break
            if death_year is not None:
                break
    except Exception:
        # Network / parse errors — don't poison cache, just return unknown.
        return None
    cache[docno] = {
        "death_year": death_year,
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_death_cache(cache)
    return death_year


# -- PRD-026: quality filter -----------------------------------------------

def quality_check(docno: str, headlines: list[dict], today: dt.date) -> tuple[bool, str]:
    """Run the four PRD-026 quality rules. Returns (ok, reason).

    Order matters; first failure wins so the reason string surfaces the
    most actionable cause."""
    if not headlines:
        return False, "no headlines"

    # 1. Stale obituary: dead-year category + obit-flavoured top
    # headlines + top headline > 30 days old.
    death_year = fetch_death_year(docno)
    if death_year is not None:
        obit_in_top3 = any(OBIT_RE.search(h.get("title", "")) for h in headlines[:3])
        try:
            top_pub = dt.datetime.fromisoformat(headlines[0]["pub_date"]).date()
            age_days = (today - top_pub).days
        except (KeyError, ValueError):
            age_days = 9999
        if obit_in_top3 and age_days > QUALITY_STALE_OBIT_DAYS:
            return False, f"stale obituary ({death_year} deaths; headline {age_days}d old)"

    # 2. Marketing pattern in top 3 headlines.
    for h in headlines[:3]:
        if MARKETING_RE.search(h.get("title", "")):
            return False, f"marketing pattern: {h['title'][:60]!r}"

    # 3. Top headline must be ≤QUALITY_HEADLINE_MAX_AGE_DAYS old.
    try:
        top_pub = dt.datetime.fromisoformat(headlines[0]["pub_date"]).date()
        age_days = (today - top_pub).days
    except (KeyError, ValueError):
        return False, "top headline has unparseable pub_date"
    if age_days > QUALITY_HEADLINE_MAX_AGE_DAYS:
        return False, f"top headline {age_days}d old (>{QUALITY_HEADLINE_MAX_AGE_DAYS}d)"

    # 4. At least one mainstream source in top 3.
    sources = [h.get("source", "") for h in headlines[:3]]
    if not any(MAINSTREAM_SOURCES_RE.search(s) for s in sources):
        return False, f"no mainstream source in top 3 (saw: {', '.join(sources) or 'none'})"

    return True, "ok"


# -- PRD-026: Google News top stories ---------------------------------------

# Cached entity title → docno reverse map. Loaded lazily from
# entity_titles.tsv (built alongside entity_classes.json by
# build_entity_set.py).
_ENTITY_TITLE_INDEX: dict[str, str] | None = None
_ENTITY_TITLE_TOKENS: set[str] | None = None


def _load_entity_title_index() -> tuple[dict[str, str], set[str]]:
    """Load entity_titles.tsv as a lowercase-title → docno map. Returns
    ({lowercased_title: docno}, set_of_all_single_word_tokens). The
    token set is used to short-circuit headlines that contain no
    plausible entity reference."""
    global _ENTITY_TITLE_INDEX, _ENTITY_TITLE_TOKENS
    if _ENTITY_TITLE_INDEX is not None and _ENTITY_TITLE_TOKENS is not None:
        return _ENTITY_TITLE_INDEX, _ENTITY_TITLE_TOKENS
    idx: dict[str, str] = {}
    tokens: set[str] = set()
    if ENTITY_TITLES_PATH.exists():
        with open(ENTITY_TITLES_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or "\t" not in line:
                    continue
                docno, display = line.split("\t", 1)
                display_lc = display.lower()
                idx.setdefault(display_lc, docno)
                # Multi-word: also keep token-level entries for fast
                # filtering, but don't override exact-title matches.
                for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]+", display):
                    if len(w) >= 3:
                        tokens.add(w.lower())
    _ENTITY_TITLE_INDEX = idx
    _ENTITY_TITLE_TOKENS = tokens
    log_dummy_cfg = None  # log() requires cfg; print here is fine before main()
    print(f"loaded entity title index: {len(idx):,} titles, "
          f"{len(tokens):,} tokens", flush=True)
    return idx, tokens


def _headline_to_docnos(headline_title: str) -> list[str]:
    """Map a Google News headline to candidate docnos. Tries longest-
    multi-word match first, then individual capitalised proper-noun
    sequences. Returns up to 3 docnos."""
    idx, tokens = _load_entity_title_index()
    if not idx:
        return []
    text = headline_title
    text_lc = text.lower()
    matched: list[str] = []
    # Try multi-word proper-noun spans: "[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"
    # (e.g. "Mark Carney", "Wes Streeting", "Bank of England").
    for m in re.finditer(r"\b[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,5}\b", text):
        span = m.group(0).strip().lower()
        if span in idx and idx[span] not in matched:
            matched.append(idx[span])
            if len(matched) >= 3:
                break
    # Also try simpler approach: any 2-4 consecutive capitalised words
    # that aren't already matched.
    return matched


def fetch_google_top_stories(today: dt.date) -> list[dict]:
    """Pull Google News top-stories RSS. Returns a list of dicts with
    title/source/pub_date/docnos (Wikipedia mappings, may be empty).
    Disk-cached for GOOGLE_TOP_CACHE_HOURS."""
    now = dt.datetime.now(dt.UTC)
    GOOGLE_TOP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if GOOGLE_TOP_CACHE_PATH.exists():
        try:
            payload = json.loads(GOOGLE_TOP_CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.strptime(payload["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            if (now - fetched_at).total_seconds() / 3600 <= GOOGLE_TOP_CACHE_HOURS:
                return payload.get("stories", [])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass

    req = urllib.request.Request(GOOGLE_TOP_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=NEWS_FETCH_TIMEOUT_S) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    stories = _parse_news_rss(data, today, max_items=GOOGLE_TOP_MAX_ITEMS)
    # Annotate each with candidate docnos.
    for s in stories[:GOOGLE_TOP_MAX_ITEMS]:
        s["docnos"] = _headline_to_docnos(s.get("title", ""))
    stories = stories[:GOOGLE_TOP_MAX_ITEMS]

    try:
        tmp = GOOGLE_TOP_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stories": stories},
            separators=(",", ":"),
        ), encoding="utf-8")
        os.replace(tmp, GOOGLE_TOP_CACHE_PATH)
    except OSError:
        pass
    return stories


# -- PRD-026: Wikipedia "In the news" portal --------------------------------

def fetch_wikipedia_itn(today: dt.date) -> list[dict]:
    """Pull the wikimedia featured-feed for today and extract the
    'news' array. Each entry has a `story` HTML blurb and a `links`
    array with full article objects. Returns a list of dicts with
    docno + display_title. 24h disk-cached."""
    now = dt.datetime.now(dt.UTC)
    WIKI_ITN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if WIKI_ITN_CACHE_PATH.exists():
        try:
            payload = json.loads(WIKI_ITN_CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.strptime(payload["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            if (now - fetched_at).total_seconds() / 3600 <= WIKI_ITN_CACHE_HOURS:
                return payload.get("items", [])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass

    url = WIKI_ITN_URL_TEMPLATE.format(year=today.year, month=today.month, day=today.day)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=NEWS_FETCH_TIMEOUT_S) as r:
            payload = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for story in payload.get("news", []) or []:
        story_html = story.get("story") or ""
        # Strip HTML for the blurb
        blurb = re.sub(r"<[^>]+>", "", story_html).strip()
        for link in story.get("links", []) or []:
            canonical = (link.get("titles") or {}).get("canonical")
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            items.append({
                "docno": canonical,
                "title": link.get("titles", {}).get("normalized") or canonical.replace("_", " "),
                "story_blurb": blurb[:280],
            })

    try:
        tmp = WIKI_ITN_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items},
            separators=(",", ":"),
        ), encoding="utf-8")
        os.replace(tmp, WIKI_ITN_CACHE_PATH)
    except OSError:
        pass
    return items


def apply_specificity_gate(items: list[dict]) -> list[dict]:
    """For each item, fetch its Wikipedia article and try to find a
    recent dated event paragraph. Items that have one gain
    event_paragraph / event_date / event_specificity fields (and
    will get a "news" knowledge panel served).

    Items that pass the pageview-shape filter but DON'T have an event
    paragraph are STILL kept on the rail — they're newsworthy by
    traffic signal alone, even if Wikipedia editors haven't documented
    a dated event yet. They just run a regular search when clicked.

    Only network failures (couldn't fetch wikitext at all) drop the
    item — we don't want to show a chip whose docno we can't verify.

    Caps the total kept at RAIL_MAX so we don't render hundreds."""
    today = dt.datetime.now(dt.UTC).date()
    kept = []
    n_checked = 0
    n_with_para_wiki = 0
    n_with_para_rss = 0
    n_without_para = 0
    n_dropped_fetch = 0
    for it in items[:MAX_CANDIDATES_TO_GATE]:
        n_checked += 1
        docno = it.get("docno") or it.get("title", "").replace(" ", "_")
        if not docno:
            n_dropped_fetch += 1
            continue
        wt = fetch_article_wikitext(docno)
        if wt is None:
            n_dropped_fetch += 1
            continue
        ev = find_event_paragraph(wt, today)
        if ev is not None:
            # Path 1: Wikipedia has a recent dated event paragraph.
            it["event_paragraph"]   = ev["paragraph"]
            it["event_date"]        = ev["event_date"]
            it["event_specificity"] = ev["specificity"]
            it["event_source"]      = "wikipedia"
            n_with_para_wiki += 1
        else:
            # Path 2 (PRD-022): try Google News RSS as a fallback.
            query = it.get("query") or it.get("title", "").lower()
            display_title = it.get("title") or query.title()
            headlines = fetch_news_headlines_cached(query, today=today)
            para = synthesise_news_paragraph(display_title, headlines)
            if para is not None:
                # Use the latest headline as event_date.
                try:
                    latest = max(dt.datetime.fromisoformat(h["pub_date"])
                                 for h in headlines).date().isoformat()
                except (KeyError, ValueError):
                    latest = today.isoformat()
                it["event_paragraph"] = para
                it["event_date"]      = latest
                it["event_source"]    = "news_rss"
                n_with_para_rss += 1
            else:
                n_without_para += 1
        kept.append(it)
        if len(kept) >= RAIL_MAX:
            break
    return kept, {
        "checked": n_checked,
        "with_para_wiki": n_with_para_wiki,
        "with_para_rss": n_with_para_rss,
        "without_para": n_without_para,
        "dropped_fetch": n_dropped_fetch,
        "kept": len(kept),
    }


def recompute_and_write() -> None:
    """Recompute current.json. PRD-026 changes:

      1. Gather candidates from THREE sources (spike pipeline,
         Google News top stories, Wikipedia In-the-news portal).
      2. Run the specificity gate (existing) on the union.
      3. Apply PRD-026 quality filter (stale-obit / marketing /
         7d-recency / mainstream-source).
      4. Source-weighted sort: Google News first, then spike, then
         Wikipedia ITN. Cap at RAIL_MAX.
    """
    today = dt.datetime.now(dt.UTC).date()
    history = read_history()
    denyset = load_user_denylist()
    payload = compute_current(history, denyset)
    spike_items = payload.get("items", []) if payload.get("mode") == "spike" else []
    log(f"pre-gate: mode={payload['mode']} spike_items={len(spike_items)}")
    _prune_news_cache()

    # Source 1: spike pipeline. Tag and keep.
    for it in spike_items:
        it["source"] = "spike"
        it["source_rank"] = it.get("rank", 9999)

    # Source 2: Google News top stories.
    google_items: list[dict] = []
    try:
        gs = fetch_google_top_stories(today)
    except Exception as e:
        log(f"google-top-stories fetch failed: {type(e).__name__}: {e}")
        gs = []
    seen_docnos = {it.get("docno") for it in spike_items if it.get("docno")}
    for rank, story in enumerate(gs, 1):
        for docno in story.get("docnos", []) or []:
            if not docno or docno in seen_docnos:
                continue
            if is_denied(docno, denyset):
                continue
            seen_docnos.add(docno)
            google_items.append({
                "query": docno.replace("_", " ").lower(),
                "title": docno.replace("_", " "),
                "docno": docno,
                "source": "google_news",
                "source_rank": rank,
                # event_paragraph populated later by the specificity gate or
                # by news-headline fallback
            })

    # Source 3: Wikipedia In-the-news.
    wiki_items: list[dict] = []
    try:
        itn = fetch_wikipedia_itn(today)
    except Exception as e:
        log(f"wiki-itn fetch failed: {type(e).__name__}: {e}")
        itn = []
    for rank, link in enumerate(itn, 1):
        docno = link.get("docno")
        if not docno or docno in seen_docnos:
            continue
        if is_denied(docno, denyset):
            continue
        seen_docnos.add(docno)
        wiki_items.append({
            "query": docno.replace("_", " ").lower(),
            "title": link.get("title") or docno.replace("_", " "),
            "docno": docno,
            "source": "wiki_itn",
            "source_rank": rank,
            "story_blurb": link.get("story_blurb"),
        })

    log(f"sources: spike={len(spike_items)} google_news={len(google_items)} wiki_itn={len(wiki_items)}")
    union = spike_items + google_items + wiki_items
    log(f"union (post-dedupe): {len(union)} candidates")

    # Specificity gate (Wikipedia event-paragraph or Google News RSS
    # fallback) — populates event_paragraph for items that have any
    # news content. Items without one stay; the quality filter below
    # will reject those that also lack headlines.
    if union:
        gated, gate_stats = apply_specificity_gate(union)
        log(f"specificity gate: checked={gate_stats['checked']} kept={gate_stats['kept']} "
            f"with_para_wiki={gate_stats['with_para_wiki']} "
            f"with_para_rss={gate_stats['with_para_rss']} "
            f"without_para={gate_stats['without_para']} "
            f"dropped_fetch={gate_stats['dropped_fetch']}")
    else:
        gated = []

    # Quality filter on the gated union. Cached headlines are already
    # in the news cache from the specificity gate's RSS-fallback pass,
    # so this is cheap.
    quality_kept: list[dict] = []
    n_stale_obit = n_marketing = n_stale_news = n_non_mainstream = n_no_headlines = 0
    for it in gated:
        docno = it.get("docno") or it.get("title", "").replace(" ", "_")
        query = it.get("query") or docno.replace("_", " ").lower()
        # Hit the cached headlines (same call the RSS fallback used).
        headlines = fetch_news_headlines_cached(query, today=today)
        ok, reason = quality_check(docno, headlines, today)
        if not ok:
            if "stale obituary" in reason:
                n_stale_obit += 1
            elif "marketing" in reason:
                n_marketing += 1
            elif "old" in reason or "unparseable" in reason:
                n_stale_news += 1
            elif "mainstream" in reason:
                n_non_mainstream += 1
            else:
                n_no_headlines += 1
            it["_filter_reject"] = reason
            continue
        # Pass; remember the top headline + filter trace.
        if headlines:
            it["top_headline"] = {
                "title": headlines[0].get("title"),
                "source": headlines[0].get("source"),
                "pub_date": headlines[0].get("pub_date"),
            }
        quality_kept.append(it)

    log(
        f"quality filter: kept={len(quality_kept)} dropped="
        f"stale_obit={n_stale_obit} marketing={n_marketing} "
        f"stale_news={n_stale_news} non_mainstream={n_non_mainstream} "
        f"no_headlines={n_no_headlines}"
    )

    # Source-weighted sort: google_news → spike → wiki_itn within each
    # source by source_rank. Then cap at RAIL_MAX.
    source_priority = {"google_news": 0, "spike": 1, "wiki_itn": 2}
    quality_kept.sort(key=lambda it: (
        source_priority.get(it.get("source", "spike"), 3),
        it.get("source_rank", 9999),
    ))
    quality_kept = quality_kept[:RAIL_MAX]

    # Build the payload to write. Mode stays "spike" for backwards
    # compatibility with the server / frontend logic (they treat "spike"
    # as "there's content; show the rail"). When everything's filtered
    # out we still write an empty items[] so the rail hides itself.
    final_mode = "spike" if quality_kept else "raw"
    payload["mode"] = final_mode
    payload["items"] = quality_kept

    write_current(payload)
    update_recently_seen(payload)
    log(
        f"wrote current.json: mode={final_mode} items={len(quality_kept)} "
        f"by_source: google={sum(1 for i in quality_kept if i.get('source')=='google_news')} "
        f"spike={sum(1 for i in quality_kept if i.get('source')=='spike')} "
        f"wiki_itn={sum(1 for i in quality_kept if i.get('source')=='wiki_itn')}"
    )


# -- compaction -------------------------------------------------------------

def compact() -> None:
    """Drop samples older than HISTORY_WINDOW_DAYS from history.jsonl."""
    if not HISTORY_PATH.exists():
        log("compact: no history yet, nothing to do")
        return
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=HISTORY_WINDOW_DAYS)
    tmp = HISTORY_PATH.with_suffix(".jsonl.tmp")
    kept = dropped = 0
    with open(HISTORY_PATH, encoding="utf-8") as src, \
         open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            try:
                rec = json.loads(line)
                t = dt.datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if t < cutoff:
                dropped += 1
                continue
            dst.write(line)
            kept += 1
    os.replace(tmp, HISTORY_PATH)
    log(f"compact: kept={kept} dropped={dropped}")


# -- main -------------------------------------------------------------------

def bootstrap(days: int, step_hours: int = 3) -> None:
    """One-shot: walk backwards over `days` days at `step_hours`
    intervals, downloading each pageview dump that's not already in
    history.jsonl. Then run the usual scorer once at the end.

    Skips hours we already have (via already_have), so this is
    safely re-runnable — if it dies mid-way, just run it again and
    it picks up where it left off.

    Default step_hours=3 matches the live timer's cadence so that
    bootstrapped samples and live samples sit on the same grid.
    Pulling 7 days at 3h-step = 56 samples, ~2.8 GB of downloads,
    typically 5-10 min on a decent connection.
    """
    start_hour = latest_complete_hour()
    total_hours = days * 24
    n_steps = total_hours // step_hours
    log(f"bootstrap: pulling {n_steps} samples over the last {days} days "
        f"({step_hours}h step) — newest first")

    pulled = skipped = failed = 0
    for i in range(n_steps):
        hour = start_hour - dt.timedelta(hours=i * step_hours)
        if already_have(hour):
            skipped += 1
            continue
        try:
            counts = fetch_dump(hour)
        except urllib.error.HTTPError as e:
            log(f"  HTTP error for {hour.isoformat()}: {e}")
            failed += 1
            continue
        if not counts:
            log(f"  no dump for {hour.isoformat()}")
            failed += 1
            continue
        top_titles = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_SAMPLE_KEEP]
        append_history(hour, top_titles)
        pulled += 1
        log(f"  [{pulled}/{n_steps}] appended {hour.isoformat()} "
            f"({len(top_titles)} titles)")

    log(f"bootstrap done: pulled={pulled} skipped={skipped} failed={failed}")

    if pulled:
        recompute_and_write()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--compact", action="store_true", help="trim history > 30 days and exit")
    p.add_argument("--bootstrap", type=int, metavar="DAYS",
                   help="one-shot: pull DAYS days of history at 3h cadence (e.g. --bootstrap 7) and exit")
    p.add_argument("--step-hours", type=int, default=3,
                   help="bootstrap sampling step in hours (default 3, matches live timer)")
    args = p.parse_args()

    if args.compact:
        compact()
        return

    if args.bootstrap:
        bootstrap(days=args.bootstrap, step_hours=args.step_hours)
        return

    result = fetch_latest_available()
    if result is None:
        # Nothing available in the last 12h. Don't touch current.json;
        # leave whatever's there for the homepage to keep serving.
        log("no dump available in the last 12h — exiting cleanly")
        return
    hour, counts = result

    # Keep top 1000 per sample to keep history.jsonl small.
    top_titles = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_SAMPLE_KEEP]
    append_history(hour, top_titles)
    log(f"appended sample for {hour.isoformat()} with {len(top_titles)} titles")

    recompute_and_write()


if __name__ == "__main__":
    main()
