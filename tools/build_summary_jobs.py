#!/usr/bin/env python3
"""
build_summary_jobs.py — PRD-018 producer.

Walks the top-N head queries from autosuggest (same approach as
intent.py), filters out navigational queries (high rank1/rank2 ratio),
and drops a `pending/<query_norm>.json` job file for each informational
query that isn't already summarised or queued.

Bulk mode (default):
  python3 tools/build_summary_jobs.py --mode bulk

The job file carries the query + top-M doc text (capped per-doc) so the
Mac Mini worker doesn't have to round-trip to prod for the inputs.

Idempotent: a query already present in summaries.map OR in pending/done/
installed/errors/ is skipped.

Runs as the `zettair` user via systemd timer (every ~4h).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


# ── config (env-overridable) ──────────────────────────────────────────────

SEARCH_URL = os.environ.get("ZET_SEARCH_URL", "http://localhost:8765")
SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR",
    "/mnt/wikipedia-source/summaries",
))
SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP",
    "/mnt/wikipedia-source/summaries.map",
))
DOCSTORE_PATH = Path(os.environ.get(
    "ZET_DOCSTORE",
    "/mnt/wikipedia-source/enwiki_top1m.docstore",
))
DOCMAP_PATH = Path(os.environ.get(
    "ZET_DOCMAP",
    "/mnt/wikipedia-source/enwiki_top1m.docmap",
))
SNIPPETS_STORE = Path(os.environ.get(
    "ZET_SNIPPETS_STORE",
    "/mnt/wikipedia-source/enwiki_top1m_snippets.store",
))
SNIPPETS_MAP = Path(os.environ.get(
    "ZET_SNIPPETS_MAP",
    "/mnt/wikipedia-source/enwiki_top1m_snippets.map",
))

# Bulk mode params
DEFAULT_TOP_N = 2000
DEFAULT_TOP_M = 5
DEFAULT_RATIO_THRESHOLD = 2.0
DEFAULT_PER_DOC_CAP = 12000

SCHEMA_VERSION = 1


# ── helpers ───────────────────────────────────────────────────────────────

def query_norm(s: str) -> str:
    """Must stay identical to server.py:query_norm."""
    return " ".join(s.lower().strip().split())


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def fetch_top_queries(base_url: str, top_k: int) -> list[tuple[str, int]]:
    """Pull global top-K (query, count) pairs from /suggest by walking
    every 2-char prefix and globally sorting by click count.

    Lifted from intent.py — same head pool as the nav-vs-info classifier.
    """
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
    prefixes = [a + b for a in alpha for b in alpha]
    seen: dict[str, int] = {}
    for prefix in prefixes:
        try:
            url = f"{base_url}/suggest?q={prefix}&n=200"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            for s in data.get("suggestions", []):
                q = s["query"]
                c = int(s["count"])
                if q not in seen or seen[q] < c:
                    seen[q] = c
        except Exception:
            continue
    return sorted(seen.items(), key=lambda x: -x[1])[:top_k]


def fetch_search(base_url: str, query: str, n: int) -> dict | None:
    """One /search call, returning the parsed JSON or None on failure."""
    url = f"{base_url}/search?q={urllib.parse.quote(query)}&n={n}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"  search failed for {query!r}: {type(e).__name__}: {e}")
        return None


# Stripped-down FlatStore reader (read-only, same shape as server.py's).
class FlatStoreRO:
    def __init__(self, store_path: Path, map_path: Path):
        self.store_path = store_path
        self.map_path = map_path
        self._map: dict = {}
        self._fd: int = -1
        self._loaded = False

    def load(self) -> bool:
        if not self.map_path.exists() or not self.store_path.exists():
            return False
        with open(self.map_path, encoding="utf-8") as f:
            self._map = json.load(f)
        self._fd = os.open(self.store_path, os.O_RDONLY)
        self._loaded = True
        return True

    def get(self, key: str) -> str | None:
        if not self._loaded:
            return None
        entry = self._map.get(key)
        if entry is None:
            return None
        offset, length = entry
        try:
            return os.pread(self._fd, length, offset).decode("utf-8", errors="replace")
        except OSError:
            return None

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def cap_doc_text(text: str, cap_bytes: int) -> str:
    """UTF-8-safe byte cap."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    cut = cap_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore").rstrip() + "…"


def existing_query_norms() -> set[str]:
    """Every query_norm currently summarised, queued, done, installed, or errored.
    A query in any of these states should NOT be re-queued."""
    seen: set[str] = set()

    # Already summarised
    if SUMMARIES_MAP.exists():
        try:
            with open(SUMMARIES_MAP, encoding="utf-8") as f:
                seen.update(json.load(f).keys())
        except Exception as e:
            log(f"  warn: couldn't read {SUMMARIES_MAP}: {e}")

    # In flight or terminal
    for sub, suffix in [
        ("pending",  ".json"),
        ("done",     ".md"),
        ("installed",".md"),
        ("errors",   ".error.json"),
    ]:
        d = SUMMARIES_DIR / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            name = p.name
            if name.endswith(suffix):
                seen.add(name[: -len(suffix)])
    return seen


def write_pending_job(query: str, score_ratio: float, click_weight: int,
                      results: list[dict], docstore: FlatStoreRO,
                      snippets: FlatStoreRO, per_doc_cap: int,
                      top_m: int) -> Path | None:
    """Build and atomically write pending/<query_norm>.json. Returns the
    final path on success, None on failure."""
    qnorm = query_norm(query)
    job_results = []
    for r in results[:top_m]:
        docno = r.get("docno", "")
        text = docstore.get(docno) or snippets.get(docno) or ""
        if not text:
            # Skip docs we can't pull text for — model needs something to ground on
            continue
        job_results.append({
            "docid": r.get("docid"),
            "rank":  r.get("rank"),
            "title": docno.replace("_", " "),
            "url":   r.get("url"),
            "score": r.get("score"),
            "text":  cap_doc_text(text, per_doc_cap),
        })
    if not job_results:
        log(f"  skip {query!r}: no doc text available")
        return None

    payload = {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "query_norm": qnorm,
        "click_weight": click_weight,
        "score_ratio": round(score_ratio, 3),
        "intent": "info",
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "bulk",
        "results": job_results,
    }

    pending_dir = SUMMARIES_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / f"{qnorm}.json"
    tmp = dest.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, dest)
    return dest


# ── main ───────────────────────────────────────────────────────────────────

def run_bulk(top_n: int, top_m: int, ratio_threshold: float, per_doc_cap: int,
             max_new: int) -> None:
    log(f"bulk: fetching top-{top_n} queries from {SEARCH_URL}")
    pairs = fetch_top_queries(SEARCH_URL, top_n)
    if not pairs:
        log("no autosuggest entries returned; aborting")
        return
    log(f"got {len(pairs)} queries (max clicks {pairs[0][1]:,}, min {pairs[-1][1]:,})")

    skip_set = existing_query_norms()
    log(f"already covered: {len(skip_set):,} queries (summarised, pending, done, installed, or errored)")

    docstore = FlatStoreRO(DOCSTORE_PATH, DOCMAP_PATH)
    snippets = FlatStoreRO(SNIPPETS_STORE, SNIPPETS_MAP)
    docstore.load()
    snippets.load()

    n_considered = n_nav = n_skipped = n_queued = 0
    t0 = time.time()
    try:
        for query, click_weight in pairs:
            qnorm = query_norm(query)
            if qnorm in skip_set:
                n_skipped += 1
                continue
            n_considered += 1

            data = fetch_search(SEARCH_URL, query, n=top_m)
            if not data:
                continue
            results = data.get("results", [])
            if len(results) < 2 or results[1].get("score", 0) <= 0:
                # Can't compute a ratio; skip rather than guessing
                continue
            r1 = results[0]["score"]
            r2 = results[1]["score"]
            ratio = r1 / r2 if r2 else float("inf")
            if ratio >= ratio_threshold:
                n_nav += 1
                continue

            dest = write_pending_job(
                query, ratio, click_weight, results,
                docstore, snippets, per_doc_cap, top_m,
            )
            if dest:
                n_queued += 1
                if n_queued >= max_new:
                    log(f"reached --max-new={max_new}; stopping")
                    break
    finally:
        docstore.close()
        snippets.close()

    elapsed = time.time() - t0
    log(
        f"done: considered={n_considered} nav-filtered={n_nav} "
        f"already-covered={n_skipped} queued={n_queued} "
        f"elapsed={elapsed:.1f}s"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="bulk", choices=["bulk"],
                   help="bulk: scan top-N head queries (only mode for now)")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                   help=f"head pool size [default {DEFAULT_TOP_N}]")
    p.add_argument("--top-m", type=int, default=DEFAULT_TOP_M,
                   help=f"top-M docs per query [default {DEFAULT_TOP_M}]")
    p.add_argument("--ratio-threshold", type=float, default=DEFAULT_RATIO_THRESHOLD,
                   help=f"rank1/rank2 cutoff for nav vs info [default {DEFAULT_RATIO_THRESHOLD}]")
    p.add_argument("--per-doc-cap", type=int, default=DEFAULT_PER_DOC_CAP,
                   help=f"per-doc text cap in bytes [default {DEFAULT_PER_DOC_CAP}]")
    p.add_argument("--max-new", type=int, default=200,
                   help="stop after queueing this many new jobs [default 200]")
    args = p.parse_args()

    if args.mode == "bulk":
        run_bulk(args.top_n, args.top_m, args.ratio_threshold,
                 args.per_doc_cap, args.max_new)
    else:
        sys.exit(f"unknown mode: {args.mode}")


if __name__ == "__main__":
    main()
