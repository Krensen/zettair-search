#!/usr/bin/env python3
"""PRD-029: snapshot day-roundup summaries into an append-only journal.

summaries.store entries can be overwritten by subsequent Mac Mini
regenerations; the timeline wants a stable archive so the rendered
"what happened today" copy does not drift week-to-week. This script
walks the events-index for each event_date, looks up <date>:day in
summaries.store, and appends a record to trending/day_summaries.jsonl
when the content hash differs from the last record for that date.

Idempotent: dedupes by (event_date, content_hash) read from the tail.

Record shape:
  {"t": "ISO8601",
   "event_date": "YYYY-MM-DD",
   "summary_md": "...markdown body...",
   "summary_hash": "8 hex chars"}

The events-index builder reads from this journal (latest record per
event_date) rather than the live summaries.store — keeps the rendered
timeline frozen at the most-recently-snapshotted version even if the
store is regenerated or compacted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))
DEFAULT_SUMMARIES_STORE = Path(os.environ.get(
    "ZET_SUMMARIES_STORE", "/mnt/wikipedia-source/summaries.store",
))
DEFAULT_SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP", "/mnt/wikipedia-source/summaries.map",
))


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def short_hash(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", errors="replace"),
                           digest_size=4).hexdigest()


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


def load_seen_tail(journal_path: Path) -> set[tuple[str, str]]:
    """Read the tail of day_summaries.jsonl and return the set of
    (event_date, summary_hash) tuples already journaled. ~64 KB tail
    is more than enough to cover several months of daily entries."""
    seen: set[tuple[str, str]] = set()
    if not journal_path.exists():
        return seen
    try:
        with open(journal_path, "rb") as f:
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
            ed = r.get("event_date")
            h  = r.get("summary_hash")
            if ed and h:
                seen.add((ed, h))
    except OSError:
        pass
    return seen


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--events-journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--day-summaries-journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "day_summaries.jsonl")
    p.add_argument("--summaries-store", type=Path,
                   default=DEFAULT_SUMMARIES_STORE)
    p.add_argument("--summaries-map", type=Path,
                   default=DEFAULT_SUMMARIES_MAP)
    args = p.parse_args()

    if not args.events_journal.exists():
        log(f"events journal not found at {args.events_journal}; nothing to do")
        return 0
    if not args.summaries_map.exists() or not args.summaries_store.exists():
        log(f"summaries store/map missing — nothing to snapshot")
        return 0

    # Unique event_dates we have any record of.
    dates: set[str] = set()
    for r in iter_journal(args.events_journal):
        ed = r.get("event_date")
        if ed:
            dates.add(ed)
    log(f"events journal has {len(dates):,} distinct dates")

    # Load summaries.map and resolve any <date>:day keys.
    try:
        with open(args.summaries_map, encoding="utf-8") as f:
            smap = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"could not read summaries.map: {e}")
        return 1
    day_keys = {k for k in smap.keys() if k.endswith(":day")}
    log(f"summaries.map has {len(day_keys):,} :day keys")

    seen = load_seen_tail(args.day_summaries_journal)
    log(f"snapshot journal has {len(seen):,} (date,hash) tuples already")

    args.day_summaries_journal.parent.mkdir(parents=True, exist_ok=True)
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    written = 0
    skipped_no_summary = 0
    fd = os.open(args.summaries_store, os.O_RDONLY)
    try:
        with open(args.day_summaries_journal, "a", encoding="utf-8") as out:
            for ed in sorted(dates, reverse=True):
                key = f"{ed}:day"
                if key not in day_keys:
                    skipped_no_summary += 1
                    continue
                entry = smap[key]
                if not (isinstance(entry, list) and len(entry) == 2):
                    continue
                offset, length = entry
                try:
                    body = os.pread(fd, length, offset).decode(
                        "utf-8", errors="replace")
                except OSError:
                    continue
                if not body:
                    continue
                h = short_hash(body)
                if (ed, h) in seen:
                    continue
                rec = {
                    "t": now_iso,
                    "event_date": ed,
                    "summary_md": body,
                    "summary_hash": h,
                }
                out.write(json.dumps(rec, separators=(",", ":"),
                                     ensure_ascii=False) + "\n")
                seen.add((ed, h))
                written += 1
    finally:
        os.close(fd)

    log(f"done: snapshotted={written} skipped_no_summary={skipped_no_summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
