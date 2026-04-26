#!/usr/bin/env python3
"""
loadtest.py — HTTP load test for the zettair search API.

Fires concurrent /search requests using a realistic query mix sampled from
the autosuggest list (weighted by click count, so popular queries appear more
often — matching real traffic distribution). Reports latency statistics:
mean, median, p75, p90, p95, p99, max, and a histogram.

Run for a fixed number of requests or a fixed duration.

Usage:
  python3 loadtest.py                              # 100 requests, 4 workers, localhost
  python3 loadtest.py --duration 600 --workers 10 # 10 min, 10 workers
  python3 loadtest.py --url https://zettair.io --duration 600 --workers 10
  python3 loadtest.py --requests 500 --workers 8
  python3 loadtest.py --queries queries.txt        # one query per line
"""

import argparse
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

FALLBACK_QUERIES = [
    ("albert einstein", 100), ("world war 2", 95), ("photosynthesis", 60),
    ("eiffel tower", 80), ("beatles", 75), ("australia", 70),
    ("black hole", 65), ("climate change", 90), ("shakespeare", 85),
    ("napoleon", 70), ("french revolution", 65), ("quantum mechanics", 55),
    ("dna", 60), ("evolution", 65), ("olympic games", 80),
    ("moon landing", 75), ("bitcoin", 85), ("artificial intelligence", 95),
    ("rome", 70), ("alexander the great", 60), ("periodic table", 55),
    ("magna carta", 50), ("leonardo da vinci", 65), ("charles darwin", 60),
    ("solar system", 70), ("renaissance", 60), ("cold war", 75),
    ("great wall of china", 65), ("amazon river", 55), ("mount everest", 60),
]


def fetch_autosuggest_queries(base_url: str, n: int) -> list[tuple[str, int]]:
    """Pull (query, count) pairs from the server's autosuggest list."""
    prefixes = list("abcdefghijklmnopqrstuvwxyz")
    pairs = []
    for prefix in prefixes:
        try:
            url = f"{base_url}/suggest?q={prefix}&n=100"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            for s in data.get("suggestions", []):
                pairs.append((s["query"], s["count"]))
        except Exception:
            pass
        if len(pairs) >= n:
            break
    return pairs[:n]


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
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (k - lo) * (s[hi] - s[lo])


def histogram(data: list[float], buckets: int = 12) -> str:
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
        lines.append(f"  {low:6.0f}–{high:<7.0f}ms │{bar:<{bar_width}}│ {c}")
    return "\n".join(lines)


def print_stats(results: list[dict], wall_s: float, total_fired: int):
    errors = sum(1 for r in results if r["error"] or r["status"] != 200)
    latencies = [r["ms"] for r in results if not r["error"] and r["status"] == 200]

    print()
    print("═" * 57)
    print("  RESULTS")
    print("═" * 57)
    print(f"  Total requests : {total_fired}")
    print(f"  Successful     : {len(latencies)}")
    print(f"  Errors         : {errors}")
    print(f"  Wall time      : {wall_s:.1f}s")
    print(f"  Throughput     : {total_fired / wall_s:.1f} req/s")

    if latencies:
        print()
        print(f"  Latency (ms):")
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

    print("═" * 57)


def run_for_duration(base_url, query_pool, weights, duration_s, workers, n_results):
    """Run workers continuously for duration_s seconds, collecting results."""
    results = []
    lock = threading.Lock()
    stop_event = threading.Event()
    total_fired = 0

    def worker():
        nonlocal total_fired
        while not stop_event.is_set():
            q = random.choices(query_pool, weights=weights, k=1)[0]
            r = do_request(base_url, q, n_results)
            with lock:
                results.append(r)
                total_fired += 1

    t_start = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()

    interval = max(10, duration_s // 10)
    next_report = interval
    try:
        while True:
            elapsed = time.perf_counter() - t_start
            if elapsed >= duration_s:
                break
            remaining = duration_s - elapsed
            if elapsed >= next_report:
                with lock:
                    n = len(results)
                    errs = sum(1 for r in results if r["error"] or r["status"] != 200)
                print(f"  {elapsed:5.0f}s elapsed — {n} requests, {errs} errors", flush=True)
                next_report += interval
            time.sleep(min(1.0, remaining))
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=15)

    wall_s = time.perf_counter() - t_start
    with lock:
        return list(results), wall_s, total_fired


def run_for_count(base_url, query_pool, weights, n_requests, workers, n_results):
    """Fire exactly n_requests, collecting results."""
    queries = random.choices(query_pool, weights=weights, k=n_requests)
    results = []
    t_start = time.perf_counter()
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(do_request, base_url, q, n_results) for q in queries]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            done += 1
            if done % max(1, n_requests // 10) == 0:
                print(f"  {done}/{n_requests} done...", flush=True)

    wall_s = time.perf_counter() - t_start
    return results, wall_s, n_requests


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url",      default="http://localhost:8765",
                        help="Base URL [default: http://localhost:8765]")
    parser.add_argument("--duration", type=int, default=None,
                        help="Run for this many seconds (overrides --requests)")
    parser.add_argument("--requests", type=int, default=100,
                        help="Total requests to fire [default: 100, ignored if --duration set]")
    parser.add_argument("--workers",  type=int, default=4,
                        help="Concurrent workers [default: 4]")
    parser.add_argument("--n",        type=int, default=10,
                        help="Results per query [default: 10]")
    parser.add_argument("--queries",  default=None,
                        help="File with one query per line (uniform weighting)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    # Build weighted query pool
    if args.queries:
        with open(args.queries) as f:
            lines = [l.strip() for l in f if l.strip()]
        query_pool = lines
        weights = [1] * len(lines)
        print(f"Loaded {len(query_pool):,} queries from {args.queries} (uniform weights)")
    else:
        print(f"Fetching queries from {base_url}/suggest ...", end=" ", flush=True)
        pairs = fetch_autosuggest_queries(base_url, 2600)  # 26 prefixes × 100
        if not pairs:
            pairs = FALLBACK_QUERIES
            print(f"(autosuggest unavailable, using {len(pairs)} built-in queries)")
        else:
            print(f"got {len(pairs):,} queries")
        query_pool = [p[0] for p in pairs]
        weights    = [p[1] for p in pairs]

    mode = f"{args.duration}s duration" if args.duration else f"{args.requests} requests"
    print(f"\nFiring {mode}, {args.workers} workers, {args.n} results/query")
    print(f"Target: {base_url}")
    print(f"Query pool: {len(query_pool):,} queries (weighted by click count)\n")

    if args.duration:
        results, wall_s, total = run_for_duration(
            base_url, query_pool, weights, args.duration, args.workers, args.n)
    else:
        results, wall_s, total = run_for_count(
            base_url, query_pool, weights, args.requests, args.workers, args.n)

    print_stats(results, wall_s, total)


if __name__ == "__main__":
    main()
