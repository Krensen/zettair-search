#!/usr/bin/env python3
"""
build_news_summary_jobs.py — PRD-021 news-spike summary producer.

Walks the items in /mnt/wikipedia-source/trending/current.json (written
by fetch_trending.py after the specificity gate). For each item that
has an event_paragraph, write a `pending/<query_norm>:news.json` job
so the Mac Mini summariser produces a news-flavoured summary.

A job is only written when one of:
  - The summary `<query_norm>:news` is missing from summaries.map.
  - It exists but is older than NEWS_REFRESH_HOURS (configurable,
    default 48h). Detected via the timestamp on the matching .md
    file in installed/, OR the offset map's modification time.

Runs as the `zettair` user via systemd timer (every 3h, offset 30
min from fetch_trending so we read a fresh current.json).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path


# ── config ─────────────────────────────────────────────────────────────────

SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR",
    "/mnt/wikipedia-source/summaries",
))
SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP",
    "/mnt/wikipedia-source/summaries.map",
))
TRENDING_CURRENT = Path(os.environ.get(
    "ZET_TRENDING_CURRENT",
    "/mnt/wikipedia-source/trending/current.json",
))
NEWS_REFRESH_HOURS = int(os.environ.get("ZET_NEWS_REFRESH_HOURS", "48"))


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_summaries_map() -> dict:
    """Read the offset map of installed summaries. Returns empty dict
    if the file is missing (first-rebuild scenario)."""
    if not SUMMARIES_MAP.exists():
        return {}
    try:
        with open(SUMMARIES_MAP, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to read summaries.map: {e}; treating as empty")
        return {}


def installed_md_path(query_norm: str, suffix: str = ":news") -> Path:
    """Path where the installer moves the .md after FlatStore install."""
    return SUMMARIES_DIR / "installed" / f"{query_norm}{suffix}.md"


def already_pending(query_norm: str, suffix: str = ":news") -> bool:
    """True if the same job is already in the pipeline anywhere."""
    name = f"{query_norm}{suffix}.json"
    for sub in ("pending", "done", "errors"):
        if (SUMMARIES_DIR / sub / name).exists():
            return True
    # Also check the .md outcome dirs in case it was just installed
    if (SUMMARIES_DIR / "done" / f"{query_norm}{suffix}.md").exists():
        return True
    return False


def needs_refresh(query_norm: str) -> tuple[bool, str]:
    """Return (needs_refresh, reason).

    True if the news summary is missing, or older than
    NEWS_REFRESH_HOURS. Uses the installed .md mtime as the freshness
    signal (it's set when the installer moves the file)."""
    composite_key = f"{query_norm}:news"
    smap = load_summaries_map()
    if composite_key not in smap:
        return True, "missing"
    md = installed_md_path(query_norm)
    if not md.exists():
        # Map has the offset but the .md was archived/deleted. The
        # FlatStore body is still valid; treat as present-and-recent
        # since we have no mtime to compare. Skip refresh.
        return False, "in map but no installed md to age-check"
    age_hrs = (dt.datetime.now().timestamp() - md.stat().st_mtime) / 3600.0
    if age_hrs > NEWS_REFRESH_HOURS:
        return True, f"stale ({age_hrs:.1f}h old)"
    return False, f"fresh ({age_hrs:.1f}h old)"


def write_job(item: dict) -> Path:
    """Write pending/<query_norm>:news.json with the event_paragraph
    as the summariser's input. Atomic via .tmp + replace."""
    query = item.get("query", "")
    query_norm = query.strip().lower() or item.get("title", "").strip().lower()
    if not query_norm:
        raise ValueError(f"item has no query/title: {item}")
    pending_dir = SUMMARIES_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{query_norm}:news.json"
    payload = {
        "schema_version": 1,
        "mode": "news-spike",
        "query": item.get("title", query),
        "query_norm": f"{query_norm}:news",
        "event_date": item.get("event_date"),
        "event_paragraph": item.get("event_paragraph"),
        # results is empty because the news-prompt branch on the
        # Mac Mini ignores it and uses event_paragraph instead.
        "results": [],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be enqueued without writing files")
    p.add_argument("--current", type=Path, default=TRENDING_CURRENT,
                   help=f"trending current.json path [{TRENDING_CURRENT}]")
    args = p.parse_args()

    if not args.current.exists():
        log(f"{args.current} not found — nothing to do")
        return

    with open(args.current, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", [])
    log(f"loaded {len(items)} trending items from {args.current}")

    n_enqueued = n_skipped_no_para = n_skipped_existing = n_skipped_pending = 0
    for it in items:
        if not it.get("event_paragraph"):
            n_skipped_no_para += 1
            continue
        query = it.get("query", "")
        query_norm = query.strip().lower()
        if not query_norm:
            continue
        if already_pending(query_norm):
            n_skipped_pending += 1
            continue
        needs, reason = needs_refresh(query_norm)
        if not needs:
            n_skipped_existing += 1
            continue
        log(f"  enqueue {query_norm}:news ({reason})")
        if not args.dry_run:
            write_job(it)
        n_enqueued += 1

    log(
        f"done: enqueued={n_enqueued} "
        f"skipped_no_para={n_skipped_no_para} "
        f"skipped_existing_fresh={n_skipped_existing} "
        f"skipped_already_pending={n_skipped_pending}"
    )


if __name__ == "__main__":
    main()
