#!/usr/bin/env python3
"""
intent.py — classify queries as navigational vs informational.

Samples queries from the autosuggest list (weighted by click count, like
loadtest.py), runs each through /search, and looks at the BM25 score
distribution across the top results. Navigational queries ("ozzy osbourne")
have one clear winner — rank1 score dominates rank2. Informational queries
("photosynthesis") spread the score more evenly across many relevant docs.

The signal is the score ratio rank1/rank2. Reports the distribution and
samples from each bucket so you can eyeball whether the threshold is sane.

Usage:
  python3 intent.py                      # 500 queries against localhost:8765
  python3 intent.py --n 200              # smaller sample
  python3 intent.py --url https://zettair.io
"""

import argparse
import json
import random
import statistics
import urllib.error
import urllib.parse
import urllib.request


def fetch_autosuggest_queries(base_url: str, target: int) -> list[tuple[str, int]]:
    """Pull (query, count) pairs from /suggest across two-letter prefixes."""
    alpha = "abcdefghijklmnopqrstuvwxyz"
    prefixes = [a + b for a in alpha for b in alpha]
    seen = set()
    pairs = []
    for prefix in prefixes:
        try:
            url = f"{base_url}/suggest?q={prefix}&n=200"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            for s in data.get("suggestions", []):
                q = s["query"]
                if q not in seen:
                    seen.add(q)
                    pairs.append((q, s["count"]))
        except Exception:
            pass
        if len(pairs) >= target:
            break
    return pairs


def fetch_scores(base_url: str, query: str, n_results: int) -> list[float]:
    url = f"{base_url}/search?q={urllib.parse.quote(query)}&n={n_results}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return [x["score"] for x in data.get("results", [])[:n_results]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8765", help="base URL of the zettair server")
    parser.add_argument("--n", type=int, default=500, help="number of queries to classify")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducibility")
    parser.add_argument("--samples-per-bucket", type=int, default=8, help="example queries to print per bucket")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Fetching autosuggest pool from {args.url}...", flush=True)
    pairs = fetch_autosuggest_queries(args.url, target=args.n * 5)
    if not pairs:
        print("No autosuggest entries returned. Is the server running?")
        return
    print(f"Got {len(pairs)} unique queries from autosuggest.", flush=True)

    queries, weights = zip(*pairs)
    sample = random.choices(queries, weights=weights, k=args.n)

    print(f"Classifying {len(sample)} queries...", flush=True)
    results = []
    failures = 0
    for q in sample:
        try:
            scores = fetch_scores(args.url, q, n_results=5)
        except Exception:
            failures += 1
            continue
        if len(scores) < 2 or scores[1] <= 0:
            continue
        r1, r2 = scores[0], scores[1]
        r3 = scores[2] if len(scores) >= 3 and scores[2] > 0 else r2
        results.append((q, r1, r2, r3, r1 / r2, r1 / r3))

    if not results:
        print("No queries returned >=2 scored results — nothing to classify.")
        return

    ratios = sorted(r[4] for r in results)
    print(f"\n=== {len(results)} queries with valid scores ({failures} request failures) ===")
    print(
        f"rank1/rank2 ratio:  median={statistics.median(ratios):.2f}  "
        f"p25={ratios[len(ratios) // 4]:.2f}  "
        f"p75={ratios[3 * len(ratios) // 4]:.2f}  "
        f"max={max(ratios):.2f}"
    )

    buckets = [
        ("strong nav (>=2.0)", 2.0, float("inf")),
        ("lean nav (1.5-2.0)", 1.5, 2.0),
        ("ambig (1.2-1.5)", 1.2, 1.5),
        ("info (<1.2)", 0.0, 1.2),
    ]
    counts = {label: 0 for label, _, _ in buckets}
    for _, _, _, _, ratio, _ in results:
        for label, lo, hi in buckets:
            if lo <= ratio < hi:
                counts[label] += 1
                break

    print("\nclassifier output:")
    for label, _, _ in buckets:
        v = counts[label]
        print(f"  {label:<22} {v:5d}  ({100.0 * v / len(results):.1f}%)")

    print("\n=== samples from each bucket ===")
    for label, lo, hi in buckets:
        print(f"\n--- {label} ---")
        samp = [r for r in results if lo <= r[4] < hi]
        random.shuffle(samp)
        for q, r1, r2, r3, ratio, _ in samp[: args.samples_per_bucket]:
            print(f"  ratio={ratio:5.2f}  r1={r1:6.2f} r2={r2:6.2f} r3={r3:6.2f}  {q}")


if __name__ == "__main__":
    main()
