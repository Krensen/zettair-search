#!/usr/bin/env python3
"""
enqueue.py — manually drop a summary job into the priority queue.

Two modes:

  ./enqueue.py "elon musk"
      Build a biographical job for the query "elon musk" using the
      live search server's results + the docstore, just like the
      bulk producer does — but write it to priority/ instead of
      pending/. Use this to re-run / refresh a specific summary
      after editing the prompt, or to push a hot query through.

  ./enqueue.py --news "tristan da cunha"
      Build a news-spike job from the article's current Wikipedia
      revision (same heuristic the trending fetcher uses). Drops
      to priority/<query>:news.json.

  ./enqueue.py --raw foo.json
      Copy a hand-built JSON file straight into priority/.

Idempotency note: this DOES NOT check if a summary already exists.
That's deliberate — the whole point of using this tool is to force
a re-run.

Runs as the zettair user (writes need to land in /mnt/.../priority).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path


SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR",
    "/mnt/wikipedia-source/summaries",
))
SEARCH_URL = os.environ.get("ZET_SEARCH_URL", "http://localhost:8765")


def query_norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def fetch_search(query: str, n: int = 5) -> dict | None:
    url = f"{SEARCH_URL}/search?q={urllib.parse.quote(query)}&n={n}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.load(r)
    except Exception as e:
        print(f"ERROR: /search call failed: {e}", file=sys.stderr)
        return None


def fetch_wiki_event(query: str) -> dict | None:
    """Pull the current article + run the trending fetcher's event-
    paragraph heuristic. Used by --news mode for a one-shot."""
    # Import lazily to keep the script usable without the trending
    # fetcher importable in all environments.
    sys.path.insert(0, str(Path(__file__).parent))
    import fetch_trending as ft   # type: ignore
    docno = query.replace(" ", "_")
    docno = docno[:1].upper() + docno[1:]
    wt = ft.fetch_article_wikitext(docno)
    if wt is None:
        print(f"ERROR: couldn't fetch wikitext for {docno}", file=sys.stderr)
        return None
    today = dt.datetime.now(dt.UTC).date()
    return ft.find_event_paragraph(wt, today)


def write_priority(payload: dict, name: str) -> Path:
    pri = SUMMARIES_DIR / "priority"
    pri.mkdir(parents=True, exist_ok=True)
    path = pri / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="query string, or path to a JSON file (with --raw)")
    p.add_argument("--news", action="store_true",
                   help="build a news-spike job from the current Wikipedia article")
    p.add_argument("--raw", action="store_true",
                   help="treat target as a path to a pre-built JSON file and copy it")
    args = p.parse_args()

    if args.raw:
        src = Path(args.target)
        if not src.exists():
            print(f"ERROR: {src} not found", file=sys.stderr); sys.exit(1)
        dst = SUMMARIES_DIR / "priority" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        print(f"enqueued raw -> {dst}")
        return

    query = args.target
    qnorm = query_norm(query)

    if args.news:
        ev = fetch_wiki_event(query)
        if ev is None:
            print(f"ERROR: no recent dated event paragraph for {query!r}", file=sys.stderr)
            sys.exit(2)
        payload = {
            "schema_version": 1,
            "mode": "news-spike",
            "query": query,
            "query_norm": f"{qnorm}:news",
            "event_date": ev["event_date"],
            "event_paragraph": ev["paragraph"],
            "results": [],
        }
        path = write_priority(payload, f"{qnorm}:news.json")
        print(f"enqueued news -> {path}")
        return

    # Biographical: replicate build_summary_jobs.py's job shape.
    data = fetch_search(query, n=5)
    if not data:
        sys.exit(1)
    results = []
    for r in data.get("results", [])[:5]:
        results.append({
            "rank": r.get("rank"),
            "docno": r.get("docno"),
            "title": r.get("docno", "").replace("_", " "),
            "text": r.get("snippet") or "",   # snippet is "good enough" without docstore
        })
    payload = {
        "schema_version": 1,
        "query": query,
        "query_norm": qnorm,
        "results": results,
    }
    path = write_priority(payload, f"{qnorm}.json")
    print(f"enqueued biographical -> {path}")


if __name__ == "__main__":
    main()
