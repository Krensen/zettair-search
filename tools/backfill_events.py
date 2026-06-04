#!/usr/bin/env python3
"""PRD-029 backfill: reconstruct historical events for the timeline.

Reads trending/history.jsonl (the append-only hourly sample log; each
record is {t, rows: [[title, views], ...]}) and reconstructs an event
journal entry for the top-N titles per day in the requested range.

For each (date, title):
  1. Query Google News RSS for the title, scoped to [date, date+2] via
     the after:/before: query qualifiers. Date range is honoured server
     side (verified empirically before this was built).
  2. If we get >= 2 headlines, synthesise an event_paragraph using the
     same helper the live pipeline uses (PRD-022 path), and write an
     events.jsonl record tagged event_source="news_rss_backfill".
  3. If we get fewer headlines, write a thinner record with
     event_paragraph=null and event_source="spike_only" — the calendar
     still shows the entity-coloured pill for that day even without
     copy.

The output is append-only and deduped at the existing journal-tail
scan in journal_events(), so re-running this script is safe.

Notes:
 - "t" is set to D + noon UTC, a faux capture time. The events-index
   builder dedupes on (docno, event_date) keeping the latest t — so
   any live capture from a later date will beat a backfill record.
   That is intentional: live data wins.
 - Backfill records are clearly labelled in event_source so we can
   filter or revert them later if quality is poor.
 - We do NOT use the Wikipedia REST API. The user direction was
   explicit: Google News only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# Reuse helpers from the production pipeline.
sys.path.insert(0, str(Path(__file__).parent))
import fetch_trending as ft  # type: ignore


HISTORY_PATH = ft.HISTORY_PATH
JOURNAL_PATH = ft.EVENTS_JOURNAL_PATH

# We query Google News with explicit after:/before:. Empirically these
# return zero-result responses sometimes for very obscure titles; that
# is fine. Date range = [D, D+2) to catch headlines published the
# morning after the spike day.
NEWS_BACKFILL_URL = (
    "https://news.google.com/rss/search?"
    "q={q}&hl=en-US&gl=US&ceid=US:en"
)
PER_DAY_TITLE_CAP   = 40    # how many distinct titles per day we attempt
HEADLINE_MIN        = 2     # below this, write the spike_only record instead
HEADLINE_MAX        = 5     # top headlines used to synthesise the paragraph
REQUEST_DELAY_S     = 0.25  # polite pause between Google requests
REQUEST_TIMEOUT_S   = 8


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# History.jsonl -> per-day candidate titles
# ---------------------------------------------------------------------------

def load_titles_per_day(start: dt.date, end: dt.date,
                        denyset: set[str]) -> dict[dt.date, list[str]]:
    """Return {date: [title, ...]} for the inclusive [start, end] range.

    Within a day, we union titles across all samples, keep the highest
    view-count per title, dedupe, apply the user + structural denylist,
    and cap at PER_DAY_TITLE_CAP."""
    if not HISTORY_PATH.exists():
        log(f"history not found at {HISTORY_PATH}; nothing to backfill")
        return {}
    per_day: dict[dt.date, dict[str, int]] = defaultdict(dict)
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("t")
            try:
                hour = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                continue
            d = hour.date()
            if d < start or d > end:
                continue
            for row in rec.get("rows") or []:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                title, views = row[0], row[1]
                if not isinstance(title, str) or not isinstance(views, int):
                    continue
                if ft.is_denied(title, denyset):
                    continue
                cur = per_day[d].get(title, 0)
                if views > cur:
                    per_day[d][title] = views
    out: dict[dt.date, list[str]] = {}
    for d, titles_dict in per_day.items():
        sorted_titles = sorted(titles_dict.items(), key=lambda kv: -kv[1])
        out[d] = [t for t, _ in sorted_titles[:PER_DAY_TITLE_CAP]]
    return out


# ---------------------------------------------------------------------------
# Google News (date-scoped) — relaxed parser
# ---------------------------------------------------------------------------

_GN_TITLE_RE = re.compile(r"^(.*?)\s+-\s+([^-]+)$")


def _parse_news_rss_relaxed(xml_bytes: bytes,
                            keep_range: tuple[dt.date, dt.date],
                            max_items: int) -> list[dict]:
    """Parse Google News RSS without the live-pipeline freshness filter.
    Keeps only items whose pubDate is inside the requested date range
    (Google sometimes returns items outside the after:/before: scope
    so we filter again here)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    lo, hi = keep_range
    items = []
    for item in root.iter("item"):
        title_raw = (item.findtext("title") or "").strip()
        pub_raw   = (item.findtext("pubDate") or "").strip()
        link      = (item.findtext("link") or "").strip()
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
        pub = ft._parse_rss_pubdate(pub_raw)
        if pub is None:
            continue
        pub_d = pub.date()
        if pub_d < lo or pub_d > hi:
            continue
        items.append({
            "title": title,
            "source": source,
            "link": link,
            "pub_date": pub.isoformat(),
        })
    items.sort(key=lambda h: h["pub_date"], reverse=True)
    return items[:max_items]


def fetch_headlines_for_date(query: str, target: dt.date) -> list[dict]:
    """Hit Google News scoped to [target, target+1]. Returns at most
    HEADLINE_MAX items."""
    # after: is inclusive; before: is exclusive. We want the day and
    # the morning after, so before: = target + 2 days.
    after  = target.isoformat()
    before = (target + dt.timedelta(days=2)).isoformat()
    qstr   = f"{query} after:{after} before:{before}"
    url    = NEWS_BACKFILL_URL.format(q=urllib.parse.quote(qstr))
    req    = urllib.request.Request(url, headers={"User-Agent": ft.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    # Filter to the requested range with a +1 day grace for late posts.
    return _parse_news_rss_relaxed(
        data,
        keep_range=(target, target + dt.timedelta(days=2)),
        max_items=HEADLINE_MAX,
    )


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------

def backfill(start: dt.date, end: dt.date, dry_run: bool = False) -> dict:
    denyset = ft.load_user_denylist()
    log(f"loaded user denylist: {len(denyset)} entries")
    log(f"reading history for {start.isoformat()} .. {end.isoformat()}")
    per_day = load_titles_per_day(start, end, denyset)
    if not per_day:
        log("no candidate titles found in history.jsonl for this range")
        return {"written": 0, "spike_only": 0, "with_para": 0, "queried": 0}

    total_titles = sum(len(v) for v in per_day.values())
    log(f"  {len(per_day)} days with candidates, {total_titles:,} total title-attempts")

    stats = {"written": 0, "spike_only": 0, "with_para": 0, "queried": 0,
             "skipped_no_headlines": 0, "errors": 0}

    # Walk dates in ascending order so the journal is roughly time-ordered.
    for d in sorted(per_day.keys()):
        titles = per_day[d]
        # Precompute {title: rank} so the per-title loop is O(1) instead
        # of O(n) per item via titles.index(). Titles are already
        # uniquified upstream in load_titles_per_day.
        title_rank = {t: i + 1 for i, t in enumerate(titles)}
        items: list[dict] = []
        log(f"backfilling {d.isoformat()} ({len(titles)} titles)")
        for title in titles:
            stats["queried"] += 1
            try:
                hls = fetch_headlines_for_date(ft.title_to_query(title), d)
            except Exception as e:
                log(f"  fetch failed for {title!r}: {type(e).__name__}: {e}")
                stats["errors"] += 1
                hls = []
            time.sleep(REQUEST_DELAY_S)
            display = ft.title_to_display(title)
            item = {
                "docno":       title,
                "title":       display,
                "event_date":  d.isoformat(),
                "source":      "spike",        # trending_source label
                "source_rank": title_rank[title],
            }
            if len(hls) >= HEADLINE_MIN:
                para = ft.synthesise_news_paragraph(display, hls)
                if para:
                    item["event_paragraph"] = para
                    item["event_source"]    = "news_rss_backfill"
                    item["top_headline"] = {
                        "title": hls[0].get("title"),
                        "source": hls[0].get("source"),
                        "pub_date": hls[0].get("pub_date"),
                    }
                    stats["with_para"] += 1
                else:
                    # synthesise refused (e.g. lines<=1) -> spike-only
                    item["event_paragraph"] = None
                    item["event_source"]    = "spike_only"
                    stats["spike_only"] += 1
            else:
                # Below the headline threshold: still write a spike_only
                # record so the calendar cell shows the colour pill.
                item["event_paragraph"] = None
                item["event_source"]    = "spike_only"
                stats["spike_only"] += 1
                stats["skipped_no_headlines"] += 1
            items.append(item)

        if dry_run:
            log(f"  [dry-run] would journal {len(items)} items for {d.isoformat()}")
        else:
            captured = dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=dt.UTC)
            # journal_events skips items with no event_paragraph; we
            # want to keep the spike-only entries too, so journal them
            # directly through a small writer that mirrors journal_events
            # but writes None paragraphs as well.
            n = _journal_backfill_items(items, captured)
            stats["written"] += n
            log(f"  -> journaled {n} new records (with_para+spike_only)")

    log(f"done: {stats}")
    return stats


def _journal_backfill_items(items: list[dict], captured_at: dt.datetime) -> int:
    """Variant of fetch_trending.journal_events that also persists
    spike-only entries (where event_paragraph is None). Same dedupe
    semantics — (docno, event_date, hash-of-empty-or-text) tuple."""
    if not items:
        return 0
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    t_iso = captured_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Tail-scan to dedupe.
    seen: set[tuple[str, str, str]] = set()
    if JOURNAL_PATH.exists():
        try:
            with open(JOURNAL_PATH, "rb") as f:
                seeked = False
                try:
                    f.seek(-65536, os.SEEK_END); seeked = True
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", errors="replace")
            lines = tail.split("\n")
            if seeked and len(lines) > 1:
                lines = lines[1:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                seen.add((
                    r.get("docno") or "",
                    r.get("event_date") or "",
                    r.get("event_paragraph_hash") or "",
                ))
        except OSError:
            pass

    written = 0
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        for it in items:
            docno = it.get("docno")
            if not docno:
                continue
            ev_date = it.get("event_date") or ""
            para = it.get("event_paragraph")
            ev_hash = ft._short_hash(para) if para else "spikeonly"
            if (docno, ev_date, ev_hash) in seen:
                continue
            rec = {
                "t":           t_iso,
                "docno":       docno,
                "title":       it.get("title") or docno.replace("_", " "),
                "event_date":  ev_date,
                "event_paragraph":      para,
                "event_paragraph_hash": ev_hash,
                "event_source":   it.get("event_source"),
                "trending_source": it.get("source"),
                "source_rank":     it.get("source_rank"),
            }
            top = it.get("top_headline")
            if top:
                rec["top_headline"] = top
            f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            seen.add((docno, ev_date, ev_hash))
            written += 1
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_d", required=True,
                   help="Inclusive start date YYYY-MM-DD")
    p.add_argument("--to", dest="to_d", required=True,
                   help="Inclusive end date YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be journaled, do not write")
    args = p.parse_args()
    try:
        start = dt.date.fromisoformat(args.from_d)
        end   = dt.date.fromisoformat(args.to_d)
    except ValueError as e:
        print(f"ERROR: invalid date: {e}", file=sys.stderr)
        return 1
    if start > end:
        print(f"ERROR: from > to", file=sys.stderr)
        return 1
    backfill(start, end, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
