#!/usr/bin/env python3
"""
compact_news_summaries.py — PRD-021 weekly news-summary compaction.

Walks summaries.map for composite keys ending in `:news`. For each,
checks the trending history.jsonl: if the subject hasn't appeared on
the trending rail in the last STALE_NEWS_DAYS, the entry is removed
from the map.

Map shrink only — the FlatStore body keeps the bytes. PRD-018's
periodic rebuild path is what reclaims disk; we just stop pointing at
news summaries the rail no longer cares about.

Acquires a flock on <summaries>/installer.lock so it doesn't race the
PRD-018 installer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
from pathlib import Path


SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR",
    "/mnt/wikipedia-source/summaries",
))
SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP",
    "/mnt/wikipedia-source/summaries.map",
))
TRENDING_HISTORY = Path(os.environ.get(
    "ZET_TRENDING_HISTORY",
    "/mnt/wikipedia-source/trending/history.jsonl",
))
STALE_NEWS_DAYS = int(os.environ.get("ZET_STALE_NEWS_DAYS", "30"))


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def title_to_query_norm(title: str) -> str:
    """Same normalisation the trending fetcher uses. Underscore → space, lower."""
    return title.replace("_", " ").lower().strip()


def queries_in_recent_trending(window_days: int) -> set[str]:
    """Return the set of query_norms seen on the trending rail in the
    last window_days. Reads history.jsonl rows (the raw [(title, views)]
    list per sample) and normalises titles to queries."""
    if not TRENDING_HISTORY.exists():
        log(f"no trending history at {TRENDING_HISTORY}; nothing fresh")
        return set()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=window_days)
    seen: set[str] = set()
    with open(TRENDING_HISTORY, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                t = dt.datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if t < cutoff:
                continue
            for row in rec.get("rows", []):
                if row and row[0]:
                    seen.add(title_to_query_norm(row[0]))
    return seen


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be dropped without modifying the map")
    p.add_argument("--window-days", type=int, default=STALE_NEWS_DAYS,
                   help=f"freshness window [{STALE_NEWS_DAYS}d]")
    args = p.parse_args()

    if not SUMMARIES_MAP.exists():
        log(f"{SUMMARIES_MAP} does not exist; nothing to do")
        return

    lock_path = SUMMARIES_DIR / "installer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"installer is running ({lock_path} held); skipping this cycle")
            return

        with open(SUMMARIES_MAP, encoding="utf-8") as f:
            smap = json.load(f)

        recent = queries_in_recent_trending(args.window_days)
        log(f"trending history yielded {len(recent):,} unique query_norms in last {args.window_days}d")

        news_keys = [k for k in smap if k.endswith(":news")]
        log(f"map has {len(smap):,} entries total, {len(news_keys):,} of which are :news")

        to_drop = []
        for key in news_keys:
            base = key[:-len(":news")]
            if base not in recent:
                to_drop.append(key)

        log(f"compaction: dropping {len(to_drop):,} stale :news entries")
        for k in to_drop[:10]:
            log(f"  drop: {k}")
        if len(to_drop) > 10:
            log(f"  ... and {len(to_drop) - 10:,} more")

        if not to_drop or args.dry_run:
            if args.dry_run:
                log("dry-run: not modifying summaries.map")
            return

        for k in to_drop:
            del smap[k]

        tmp = SUMMARIES_MAP.with_suffix(".map.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(smap, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, SUMMARIES_MAP)
        log(f"wrote {SUMMARIES_MAP} with {len(smap):,} entries")
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
