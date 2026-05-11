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
MAX_CANDIDATES_TO_GATE  = 50    # cap API fan-out per fetch
MIN_PARA_LEN            = 120   # chars; below this is too short to be a real event para
# Loosened after first prod run: with 56h of bootstrap history and a
# narrow candidate pool, the strict gate (score>=4, fresh<=14d) found
# zero matches across all 7 spikers and the rail went empty. We accept
# a few more false positives in exchange for a non-empty rail. Will
# tighten back once the history window naturally widens.
MIN_SPECIFICITY_SCORE   = 2     # was 4 — allows month-precision dates alone to qualify
EVENT_FRESHNESS_DAYS    = 30    # was 14 — admits month-old events
EVENT_PARA_MAX_CHARS    = 2000  # truncate event_paragraph stored in current.json
TOP_SAMPLE_KEEP         = 3000  # widened from 1000 — gives specificity gate filter surface

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


def apply_specificity_gate(items: list[dict]) -> list[dict]:
    """For each item, fetch its Wikipedia article and look for a recent
    dated event paragraph. Drop items that fail. Items that pass gain
    event_paragraph / event_date / event_specificity fields.

    Operates in input order (already sorted by spike score). Caps at
    MAX_CANDIDATES_TO_GATE so API fan-out stays bounded."""
    today = dt.datetime.now(dt.UTC).date()
    kept = []
    n_checked = 0
    n_dropped_no_para = 0
    n_dropped_fetch = 0
    for it in items[:MAX_CANDIDATES_TO_GATE]:
        n_checked += 1
        docno = it.get("docno") or it.get("title", "").replace(" ", "_")
        if not docno:
            n_dropped_no_para += 1
            continue
        wt = fetch_article_wikitext(docno)
        if wt is None:
            n_dropped_fetch += 1
            continue
        ev = find_event_paragraph(wt, today)
        if ev is None:
            n_dropped_no_para += 1
            continue
        it["event_paragraph"]   = ev["paragraph"]
        it["event_date"]        = ev["event_date"]
        it["event_specificity"] = ev["specificity"]
        kept.append(it)
        if len(kept) >= RAIL_MAX:
            break
    return kept, {
        "checked": n_checked,
        "dropped_no_para": n_dropped_no_para,
        "dropped_fetch": n_dropped_fetch,
        "kept": len(kept),
    }


def recompute_and_write() -> None:
    """Recompute current.json from history + apply specificity gate."""
    history = read_history()
    denyset = load_user_denylist()
    payload = compute_current(history, denyset)
    log(f"pre-gate: mode={payload['mode']} items={len(payload['items'])}")
    if payload["mode"] == "spike" and payload["items"]:
        kept, stats = apply_specificity_gate(payload["items"])
        log(f"specificity gate: checked={stats['checked']} "
            f"kept={stats['kept']} dropped_no_para={stats['dropped_no_para']} "
            f"dropped_fetch={stats['dropped_fetch']}")
        payload["items"] = kept
    write_current(payload)
    log(f"wrote current.json: mode={payload['mode']} items={len(payload['items'])}")


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
