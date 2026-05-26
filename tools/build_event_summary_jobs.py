#!/usr/bin/env python3
"""PRD-029: enqueue LLM event-summary jobs for the timeline.

For each (docno, event_date) in the events journal that does not yet
have a corresponding summary in summaries.store, drop a job into
summaries/priority/<docno>:<event_date>:event.json. The Mac Mini
drains priority/, runs the news-spike prompt against the
event_paragraph, drops the .md back into done/, the installer puts
it in summaries.store under key `<docno>:<event_date>:event`.

This is the heaviest of the PRD-029 step-3 pieces — generating
hundreds of summaries for the May backfill costs Mac Mini cycles.
Run it manually once after the backfill; let it cook overnight.
Subsequent live event captures are enqueued by the same script via
a future hook in fetch_trending.py (not in this commit).

Job key namespace:
  <docno>:<event_date>:event   — distinct from PRD-021's
                                  <query_norm>:news so the two never
                                  collide in the same FlatStore.

Idempotent: skips events whose summary is already in summaries.map,
or whose job is already pending/done/installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

# Mirror of fetch_trending.title_to_query (kept inline so this script
# does not import the whole trending module). Used to derive a
# query_norm from a docno so we can check whether a <query_norm>:news
# summary already exists in summaries.map.
_DISAMB_PAREN = re.compile(r"_\([^()]+\)$")
_TRAILING_PUNCT = re.compile(r"[\.,!?]+$")


def _query_norm_from_docno(docno: str) -> str:
    t = _DISAMB_PAREN.sub("", docno)
    t = t.replace("_", " ")
    t = urllib.parse.unquote(t)
    t = _TRAILING_PUNCT.sub("", t)
    t = " ".join(t.split())
    return t.lower()


DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))
DEFAULT_SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR", "/mnt/wikipedia-source/summaries",
))
DEFAULT_SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP", "/mnt/wikipedia-source/summaries.map",
))


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def event_summary_key(docno: str, event_date: str) -> str:
    return f"{docno}:{event_date}:event"


def load_summaries_map(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def already_in_pipeline(summaries_dir: Path, key: str) -> bool:
    """Return True if a job for this key is anywhere in the queue."""
    name = f"{key}.json"
    for sub in ("priority", "pending", "done", "errors", "installed"):
        if (summaries_dir / sub / name).exists():
            return True
    if (summaries_dir / "done"      / f"{key}.md").exists():
        return True
    if (summaries_dir / "installed" / f"{key}.md").exists():
        return True
    return False


def write_job(summaries_dir: Path, key: str, rec: dict) -> Path:
    """Write priority/<key>.json — same shape as PRD-021 news jobs so
    the Mac Mini code path does not need to change."""
    priority_dir = summaries_dir / "priority"
    priority_dir.mkdir(parents=True, exist_ok=True)
    path = priority_dir / f"{key}.json"
    payload = {
        "schema_version": 1,
        "mode": "news-spike",   # reuse the existing news prompt path
        "query": rec.get("title") or rec.get("docno", "").replace("_", " "),
        "query_norm": key,
        "docno": rec.get("docno"),
        "event_date": rec.get("event_date"),
        "event_paragraph": rec.get("event_paragraph"),
        # results is empty; the news prompt uses event_paragraph instead.
        "results": [],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return path


def iter_journal(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--summaries-dir", type=Path,
                   default=DEFAULT_SUMMARIES_DIR)
    p.add_argument("--summaries-map", type=Path,
                   default=DEFAULT_SUMMARIES_MAP)
    p.add_argument("--max-enqueue", type=int, default=500,
                   help="cap total enqueues this run")
    p.add_argument("--require-paragraph", action="store_true",
                   help="skip events with no event_paragraph (recommended; "
                        "the LLM has nothing to summarise from a bare title)")
    p.add_argument("--news-reuse-days", type=int, default=14,
                   help="Skip enqueueing when a <query_norm>:news summary "
                        "already exists in summaries.map AND the event_date "
                        "is within this many days of today. Avoids generating "
                        "a parallel :event summary for the same incident "
                        "the search panel is already summarising.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.journal.exists():
        log(f"journal not found at {args.journal}; nothing to do")
        return 0

    smap = load_summaries_map(args.summaries_map)
    log(f"summaries.map has {len(smap):,} existing entries")
    today_iso = dt.datetime.now(dt.UTC).date().isoformat()
    news_floor_iso = (dt.datetime.now(dt.UTC).date()
                      - dt.timedelta(days=args.news_reuse_days)).isoformat()

    # Walk the journal and dedupe to one record per (docno, event_date)
    # keeping the latest captured `t`.
    latest: dict[str, dict] = {}
    for rec in iter_journal(args.journal):
        docno = rec.get("docno")
        ed    = rec.get("event_date")
        if not docno or not ed:
            continue
        key = event_summary_key(docno, ed)
        prev = latest.get(key)
        if prev is None or (rec.get("t", "") > prev.get("t", "")):
            latest[key] = rec

    log(f"journal has {len(latest):,} unique (docno, event_date) pairs")

    stats = {"queued": 0, "in_map": 0, "in_pipeline": 0,
             "news_reused": 0, "no_paragraph": 0,
             "scanned": len(latest)}
    for key, rec in latest.items():
        if stats["queued"] >= args.max_enqueue:
            log(f"hit --max-enqueue cap ({args.max_enqueue}); stopping")
            break
        if key in smap:
            stats["in_map"] += 1
            continue
        docno  = rec.get("docno", "")
        ev_date = rec.get("event_date", "")
        # If a PRD-021 :news summary already exists for this entity
        # AND the event is recent, the events-index will fall back to
        # it at join time. No need to generate a parallel :event.
        if ev_date >= news_floor_iso:
            qn = _query_norm_from_docno(docno)
            if f"{qn}:news" in smap:
                stats["news_reused"] += 1
                continue
        if args.require_paragraph and not rec.get("event_paragraph"):
            stats["no_paragraph"] += 1
            continue
        if already_in_pipeline(args.summaries_dir, key):
            stats["in_pipeline"] += 1
            continue
        if args.dry_run:
            log(f"  [dry-run] would enqueue {key}")
        else:
            write_job(args.summaries_dir, key, rec)
        stats["queued"] += 1

    log(f"done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
