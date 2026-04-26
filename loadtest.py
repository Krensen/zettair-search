#!/usr/bin/env python3
"""
loadtest.py — HTTP load test for the zettair search API.

Fires concurrent /search requests using a realistic query mix sampled from
the autosuggest list (or a built-in fallback set), then prints latency
statistics: mean, median, p75, p90, p95, p99, max, and a histogram.

Usage:
  python3 loadtest.py                          # 100 requests, 4 workers, localhost
  python3 loadtest.py --requests 500 --workers 8
  python3 loadtest.py --url https://zettair.io --requests 200 --workers 4
  python3 loadtest.py --queries queries.txt    # one query per line
"""

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

FALLBACK_QUERIES = [
    "albert einstein", "world war 2", "photosynthesis", "eiffel tower",
    "beatles", "australia", "black hole", "climate change", "shakespeare",
    "napoleon", "french revolution", "quantum mechanics", "dna", "evolution",
    "olympic games", "moon landing", "bitcoin", "artificial intelligence",
    "rome", "alexander the great", "periodic table", "magna carta",
    "leonardo da vinci", "charles darwin", "solar system", "renaissance",
    "cold war", "great wall of china", "amazon river", "mount everest",
]


def fetch_autosuggest_queries(base_url: str, n: int) -> list[str]:
    """Pull queries from the server's autosuggest list via a few prefix calls."""
    prefixes = list("abcdefghijklmnopqrstuvwxyz")
    queries = []
    for prefix in prefixes:
        try:
            url = f"{base_url}/suggest?q={prefix}&n=50"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            for s in data.get("suggestions", []):
                queries.append(s["query"])
        except Exception:
            pass
        if len(queries) >= n:
            break
    return queries[:n]


def do_request(base_url: str, query: str, n_results: int) -> dict:
    params = urlencode({"q": query, "n": n_results})
    url = f"{base_url}/search?{params}"
    t0 = time.perf_counter()
    status = 0
    error = None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            status = r.status
            r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        error = str(e)
    except Exception as e:
        status = 0
        error = str(e)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {"query": query, "status": status, "ms": elapsed_ms, "error": error}


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (k - lo) * (sorted_data[hi] - sorted_data[lo])


def histogram(data: list[float], buckets: int = 10) -> str:
    if not data:
        return ""
    lo, hi = min(data), max(data)
    if lo == hi:
        return f"  all values = {lo:.1f}ms"
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in data:
        idx = min(int((v - lo) / width), buckets - 1)
        counts[idx] += 1
    bar_max = max(counts)
    bar_width = 40
    lines = []
    for i, c in enumerate(counts):
        low = lo + i * width
        high = low + width
        bar = "█" * int(bar_width * c / bar_max) if bar_max else ""
        lines.append(f"  {low:6.0f}–{high:<6.0f}ms │{bar:<{bar_width}}│ {c}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url",      default="http://localhost:8765",
                        help="Base URL of the search server [default: http://localhost:8765]")
    parser.add_argument("--requests", type=int, default=100,
                        help="Total number of search requests to fire [default: 100]")
    parser.add_argument("--workers",  type=int, default=4,
                        help="Concurrent workers [default: 4]")
    parser.add_argument("--n",        type=int, default=10,
                        help="Results per query [default: 10]")
    parser.add_argument("--queries",  default=None,
                        help="Path to file with one query per line (default: fetch from autosuggest)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    # Build query pool
    if args.queries:
        with open(args.queries) as f:
            query_pool = [l.strip() for l in f if l.strip()]
        print(f"Loaded {len(query_pool):,} queries from {args.queries}")
    else:
        print(f"Fetching queries from {base_url}/suggest ...", end=" ", flush=True)
        query_pool = fetch_autosuggest_queries(base_url, 500)
        if not query_pool:
            query_pool = FALLBACK_QUERIES
            print(f"(autosuggest unavailable, using {len(query_pool)} built-in queries)")
        else:
            print(f"got {len(query_pool):,} queries")

    # Sample queries for the run
    queries = [random.choice(query_pool) for _ in range(args.requests)]

    print(f"\nFiring {args.requests} requests, {args.workers} workers, {args.n} results each...")
    print(f"Target: {base_url}\n")

    results = []
    errors = 0
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(do_request, base_url, q, args.n): q for q in queries}
        done = 0
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            done += 1
            if r["error"] or r["status"] != 200:
                errors += 1
            if done % max(1, args.requests // 10) == 0:
                print(f"  {done}/{args.requests} done...", flush=True)

    wall_s = time.perf_counter() - t_start
    latencies = [r["ms"] for r in results if not r["error"] and r["status"] == 200]

    print()
    print("═" * 55)
    print("  RESULTS")
    print("═" * 55)
    print(f"  Total requests : {args.requests}")
    print(f"  Successful     : {len(latencies)}")
    print(f"  Errors         : {errors}")
    print(f"  Wall time      : {wall_s:.1f}s")
    print(f"  Throughput     : {args.requests / wall_s:.1f} req/s")

    if latencies:
        print()
        print(f"  Latency (ms)   :")
        print(f"    mean         : {statistics.mean(latencies):.1f}")
        print(f"    median (p50) : {percentile(latencies, 50):.1f}")
        print(f"    p75          : {percentile(latencies, 75):.1f}")
        print(f"    p90          : {percentile(latencies, 90):.1f}")
        print(f"    p95          : {percentile(latencies, 95):.1f}")
        print(f"    p99          : {percentile(latencies, 99):.1f}")
        print(f"    max          : {max(latencies):.1f}")
        print()
        print("  Latency distribution:")
        print(histogram(latencies))

    if errors:
        print()
        print("  Sample errors:")
        shown = 0
        for r in results:
            if r["error"] or r["status"] != 200:
                print(f"    [{r['status']}] {r['query']!r}: {r['error']}")
                shown += 1
                if shown >= 5:
                    break

    print("═" * 55)


if __name__ == "__main__":
    main()
