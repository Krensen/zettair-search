#!/usr/bin/env python3
"""PRD-029: fetch Wikipedia categories for the entities in the events
journal. Persists results to trending/wikipedia_categories.json keyed
by docno, value is a list of category names (without the "Category:"
prefix).

Idempotent + incremental: only fetches docnos that are not already
in the cache file. Run after the journal grows (e.g. nightly or
after a backfill).

Wikimedia API is friendly to batched requests (up to 50 titles per
query); we hit it ~1 request per ~50 entities with a small inter-
request sleep. For a journal with ~1000 unique entities that is ~20
API calls, ~30 seconds wall time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))

API_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "ZettairSearch/1.0 (https://zettair.io; hugh@viaaltoadvisors.com)"
BATCH_SIZE = 50          # Wikimedia's max per query
INTER_BATCH_SLEEP = 0.25
TIMEOUT_S = 15


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


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


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "categories": {}, "missing": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Ensure shape.
        if "categories" not in data:
            data["categories"] = {}
        if "missing" not in data:
            data["missing"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "categories": {}, "missing": []}


def fetch_batch(titles: list[str]) -> dict[str, list[str] | None]:
    """Returns {docno: [cat, ...]} or {docno: None} for missing pages.

    Categories arrive without the "Category:" prefix. Limited to the
    50 displayed (non-hidden) categories per page; sufficient for
    bucket-mapping.
    """
    params = {
        "action": "query",
        "prop": "categories",
        "clshow": "!hidden",
        "cllimit": 50,
        "format": "json",
        "titles": "|".join(titles),
    }
    url = API_URL + "?" + urllib.parse.urlencode(params, safe="|")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log(f"  batch fetch failed: {type(e).__name__}: {e}")
        return {}
    # The API normalises titles (underscores -> spaces); reverse the
    # normalisation so we key results by the docno the caller passed.
    norm_map = {}
    for n in data.get("query", {}).get("normalized", []) or []:
        norm_map[n.get("to")] = n.get("from")
    out: dict[str, list[str] | None] = {}
    for page in (data.get("query", {}).get("pages") or {}).values():
        title = page.get("title")
        docno = norm_map.get(title, title or "").replace(" ", "_")
        if "missing" in page:
            out[docno] = None
            continue
        cats = []
        for c in page.get("categories", []) or []:
            ct = c.get("title", "")
            if ct.startswith("Category:"):
                cats.append(ct[len("Category:"):])
        out[docno] = cats
    return out


def build(journal_path: Path, cache_path: Path,
          force: bool = False) -> dict:
    if not journal_path.exists():
        log(f"ERROR: journal not found at {journal_path}")
        return {}
    cache = load_cache(cache_path)
    known = set(cache["categories"].keys())
    known.update(cache.get("missing", []))   # don't retry confirmed misses

    # Unique docnos in the journal.
    docnos: list[str] = []
    seen = set()
    for r in iter_journal(journal_path):
        d = r.get("docno")
        if d and d not in seen:
            seen.add(d)
            docnos.append(d)
    log(f"journal has {len(docnos):,} unique docnos")

    if force:
        todo = docnos
    else:
        todo = [d for d in docnos if d not in known]
    log(f"  {len(todo):,} need fetching ({len(docnos) - len(todo):,} cached)")

    if not todo:
        return cache

    t0 = time.time()
    fetched = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        results = fetch_batch(batch)
        for docno in batch:
            res = results.get(docno)
            if res is None:
                cache["missing"].append(docno)
            elif isinstance(res, list):
                cache["categories"][docno] = res
                fetched += 1
        elapsed = time.time() - t0
        log(f"  [{elapsed:5.1f}s] {min(i + BATCH_SIZE, len(todo))}/{len(todo)} processed")
        # Persist incrementally so we never lose progress on a crash.
        _atomic_write(cache_path, cache)
        time.sleep(INTER_BATCH_SLEEP)

    cache["built_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write(cache_path, cache)
    log(f"done: fetched={fetched} cache_size={len(cache['categories'])} "
        f"missing={len(cache.get('missing', []))}")
    return cache


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--cache", type=Path,
                   default=DEFAULT_TRENDING_DIR / "wikipedia_categories.json")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch every docno, ignoring cache")
    args = p.parse_args()
    build(args.journal, args.cache, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
