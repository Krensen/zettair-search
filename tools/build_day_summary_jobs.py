#!/usr/bin/env python3
"""PRD-029: enqueue "what happened today" day-roundup summary jobs.

For each unique event_date in the journal that does not yet have a
<YYYY-MM-DD>:day entry in summaries.store, drop a job into
summaries/priority/<YYYY-MM-DD>:day.json.

The job payload is a bundle of the day's top events — paragraph,
title, category, rank — sorted by rank_hint desc and capped at
DAY_TOP_N to keep prompt size bounded. The Mac Mini learns a new
mode: "day-roundup" prompt that takes the bundle and returns ~3-5
sentences of editorial "what mattered today" copy.

Key namespace:
  <YYYY-MM-DD>:day   — distinct from <query_norm>:news and
                       <docno>:<event_date>:event.

Idempotent: skips dates whose summary is already in summaries.map,
or whose job is already pending/done/installed.

Skips days with fewer than DAY_MIN_EVENTS events with paragraphs;
asking for a "roundup" of 2 events is weak and the LLM struggles.

Note: this script only enqueues. The Mac Mini must implement the
"day-roundup" prompt mode before jobs drain. Until then, queued
jobs sit harmlessly in priority/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))
DEFAULT_SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR", "/mnt/wikipedia-source/summaries",
))
DEFAULT_SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP", "/mnt/wikipedia-source/summaries.map",
))

DAY_TOP_N        = 20    # most-important events bundled into the prompt
DAY_MIN_EVENTS   = 5     # below this we do not bother — too thin


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def day_summary_key(ev_date: str) -> str:
    return f"{ev_date}:day"


def load_summaries_map(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def already_in_pipeline(summaries_dir: Path, key: str) -> bool:
    name = f"{key}.json"
    for sub in ("priority", "pending", "done", "errors", "installed"):
        if (summaries_dir / sub / name).exists():
            return True
    if (summaries_dir / "done"      / f"{key}.md").exists():
        return True
    if (summaries_dir / "installed" / f"{key}.md").exists():
        return True
    return False


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


def collect_per_day(journal_path: Path) -> dict[str, list[dict]]:
    """Read the journal, dedupe to one record per (docno, event_date)
    keeping the latest captured_at, group by event_date, return as
    {date: [records...]}."""
    latest: dict[tuple[str, str], dict] = {}
    for rec in iter_journal(journal_path):
        docno = rec.get("docno")
        ed    = rec.get("event_date")
        if not docno or not ed:
            continue
        key = (docno, ed)
        prev = latest.get(key)
        if prev is None or (rec.get("t", "") > prev.get("t", "")):
            latest[key] = rec

    per_day: dict[str, list[dict]] = defaultdict(list)
    for (_, ed), rec in latest.items():
        per_day[ed].append(rec)
    return per_day


def rank_hint_for(rec: dict) -> float:
    """Lightweight rank estimator mirroring build_events_index. We do
    not have entity_class here so use a simpler heuristic."""
    src_score = {"google_news": 3.0, "spike": 2.0,
                 "wiki_itn": 1.5}.get(rec.get("trending_source") or "", 1.0)
    sr = rec.get("source_rank") or 99
    rank_score = 1.0 / max(int(sr), 1) ** 0.5
    return src_score * rank_score


def write_job(summaries_dir: Path, key: str, ev_date: str,
              records: list[dict]) -> Path:
    """Write priority/<key>.json carrying the top-N events for the day.

    The Mac Mini parses these as a "day-roundup" mode: each results[]
    entry has title + text (the event_paragraph) + category."""
    priority_dir = summaries_dir / "priority"
    priority_dir.mkdir(parents=True, exist_ok=True)
    path = priority_dir / f"{key}.json"
    # Sort by rank_hint desc and cap.
    sorted_recs = sorted(records, key=lambda r: -rank_hint_for(r))[:DAY_TOP_N]
    bundle = []
    for r in sorted_recs:
        para = r.get("event_paragraph")
        if not para:
            continue
        bundle.append({
            "title":    r.get("title") or r.get("docno", "").replace("_", " "),
            "docno":    r.get("docno"),
            "text":     para,
            "category": r.get("category"),
            "rank":     round(rank_hint_for(r), 3),
        })
    payload = {
        "schema_version": 1,
        "mode": "day-roundup",
        "query": f"What happened on {ev_date}",
        "query_norm": key,
        "event_date": ev_date,
        "results": bundle,
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--summaries-dir", type=Path,
                   default=DEFAULT_SUMMARIES_DIR)
    p.add_argument("--summaries-map", type=Path,
                   default=DEFAULT_SUMMARIES_MAP)
    p.add_argument("--max-enqueue", type=int, default=120,
                   help="cap total enqueues this run; default covers ~4 months")
    p.add_argument("--horizon-days", type=int, default=120,
                   help="only consider event dates within the last N days")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.journal.exists():
        log(f"journal not found at {args.journal}; nothing to do")
        return 0

    smap = load_summaries_map(args.summaries_map)
    log(f"summaries.map has {len(smap):,} existing entries")

    today = dt.datetime.now(dt.UTC).date()
    floor = (today - dt.timedelta(days=args.horizon_days)).isoformat()
    per_day = collect_per_day(args.journal)
    log(f"journal has {len(per_day):,} distinct event dates")

    stats = {"queued": 0, "in_map": 0, "in_pipeline": 0,
             "too_thin": 0, "out_of_horizon": 0, "scanned": 0}
    # Walk dates newest-first.
    for ev_date in sorted(per_day.keys(), reverse=True):
        stats["scanned"] += 1
        if ev_date < floor:
            stats["out_of_horizon"] += 1
            continue
        if stats["queued"] >= args.max_enqueue:
            log(f"hit --max-enqueue ({args.max_enqueue}); stopping")
            break
        key = day_summary_key(ev_date)
        if key in smap:
            stats["in_map"] += 1
            continue
        records = per_day[ev_date]
        with_para = [r for r in records if r.get("event_paragraph")]
        if len(with_para) < DAY_MIN_EVENTS:
            stats["too_thin"] += 1
            continue
        if already_in_pipeline(args.summaries_dir, key):
            stats["in_pipeline"] += 1
            continue
        if args.dry_run:
            log(f"  [dry-run] would enqueue {key} ({len(with_para)} events)")
        else:
            write_job(args.summaries_dir, key, ev_date, with_para)
        stats["queued"] += 1

    log(f"done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
