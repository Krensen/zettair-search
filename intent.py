#!/usr/bin/env python3
"""
intent.py — classify queries as navigational vs informational.

Pulls the global top-K queries from /suggest (the same head pool we'll be
generating knowledge-panel summaries for), runs each through /search, and
looks at the BM25 score distribution across the top results. Navigational
queries ("ozzy osbourne") have one clear winner — rank1 score dominates
rank2. Informational queries ("photosynthesis") spread the score more
evenly across many relevant docs.

The signal is the score ratio rank1/rank2. Reports the distribution and
samples from each bucket so you can eyeball whether the threshold is sane.

Usage:
  python3 intent.py                      # head 2000, classify all
  python3 intent.py --top 5000 --n 500   # bigger pool, sub-sample 500
  python3 intent.py --url https://zettair.io
"""

import argparse
import json
import random
import statistics
import urllib.error
import urllib.parse
import urllib.request


def fetch_top_queries(base_url: str, top_k: int) -> list[tuple[str, int]]:
    """Pull global top-K (query, count) pairs from /suggest.

    /suggest only returns by-count results within a 2-char prefix, so we
    walk every prefix (aa..zz, plus digits as a safety net), union the
    results, and globally sort by click count. Stopping early would bias
    toward early-alphabet prefixes — exactly what we don't want.
    """
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
    prefixes = [a + b for a in alpha for b in alpha]
    seen = {}
    for prefix in prefixes:
        try:
            url = f"{base_url}/suggest?q={prefix}&n=200"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            for s in data.get("suggestions", []):
                q = s["query"]
                # /suggest already returns highest-count first per prefix,
                # but a query can appear under multiple prefixes only if
                # we vary the prefix scheme — keep the max defensively.
                c = s["count"]
                if q not in seen or seen[q] < c:
                    seen[q] = c
        except Exception:
            pass
    pairs = sorted(seen.items(), key=lambda x: -x[1])
    return pairs[:top_k]


def fetch_results(base_url: str, query: str, n_results: int) -> list[dict]:
    url = f"{base_url}/search?q={urllib.parse.quote(query)}&n={n_results}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data.get("results", [])[:n_results]


def normalise(s: str) -> str:
    return " ".join(s.lower().split())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8765", help="base URL of the zettair server")
    parser.add_argument("--top", type=int, default=2000, help="size of the head pool to consider (matches knowledge-panel target)")
    parser.add_argument("--n", type=int, default=0, help="number of queries to classify (0 = classify the whole top pool)")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducibility")
    parser.add_argument("--samples-per-bucket", type=int, default=8, help="example queries to print per bucket")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Fetching top {args.top} queries from {args.url}...", flush=True)
    pairs = fetch_top_queries(args.url, top_k=args.top)
    if not pairs:
        print("No autosuggest entries returned. Is the server running?")
        return
    min_count = pairs[-1][1]
    max_count = pairs[0][1]
    print(f"Got {len(pairs)} queries. Click counts: max={max_count}, min={min_count}.", flush=True)

    queries = [q for q, _ in pairs]
    if args.n and args.n < len(queries):
        weights = [c for _, c in pairs]
        sample = random.choices(queries, weights=weights, k=args.n)
    else:
        sample = queries

    print(f"Classifying {len(sample)} queries...", flush=True)
    rows = []
    failures = 0
    for q in sample:
        try:
            res = fetch_results(args.url, q, n_results=5)
        except Exception:
            failures += 1
            continue
        if len(res) < 2 or res[1].get("score", 0) <= 0:
            continue
        r1, r2 = res[0]["score"], res[1]["score"]
        title1 = res[0].get("title", "") or ""
        title_match = normalise(title1) == normalise(q)
        ratio = r1 / r2
        rows.append({"q": q, "r1": r1, "r2": r2, "ratio": ratio, "title": title1, "title_match": title_match})

    if not rows:
        print("No queries returned >=2 scored results — nothing to classify.")
        return

    ratios = sorted(r["ratio"] for r in rows)
    title_matches = sum(1 for r in rows if r["title_match"])
    print(f"\n=== {len(rows)} queries with valid scores ({failures} request failures) ===")
    print(
        f"rank1/rank2 ratio:  median={statistics.median(ratios):.2f}  "
        f"p25={ratios[len(ratios) // 4]:.2f}  "
        f"p75={ratios[3 * len(ratios) // 4]:.2f}  "
        f"max={max(ratios):.2f}"
    )
    print(f"rank-1 title is exact-match for query in {title_matches}/{len(rows)} cases ({100.0 * title_matches / len(rows):.1f}%)")

    # Classification: nav if (a) rank-1 title is exact match for query, or
    # (b) score ratio >= 1.05. Otherwise fall back to score-ratio buckets.
    def bucket_of(r):
        if r["title_match"]:
            return "nav (title-match)"
        if r["ratio"] >= 1.05:
            return "lean nav (ratio>=1.05)"
        if r["ratio"] >= 1.02:
            return "ambig (1.02-1.05)"
        return "info (<1.02)"

    bucket_order = ["nav (title-match)", "lean nav (ratio>=1.05)", "ambig (1.02-1.05)", "info (<1.02)"]
    counts = {b: 0 for b in bucket_order}
    for r in rows:
        counts[bucket_of(r)] += 1

    print("\nclassifier output:")
    for label in bucket_order:
        v = counts[label]
        print(f"  {label:<28} {v:5d}  ({100.0 * v / len(rows):.1f}%)")

    print("\n=== samples from each bucket ===")
    for label in bucket_order:
        print(f"\n--- {label} ---")
        samp = [r for r in rows if bucket_of(r) == label]
        random.shuffle(samp)
        for r in samp[: args.samples_per_bucket]:
            tm = "T" if r["title_match"] else " "
            print(f"  [{tm}] ratio={r['ratio']:5.2f}  r1={r['r1']:6.2f} r2={r['r2']:6.2f}  q={r['q']!r:<35}  title={r['title']!r}")


if __name__ == "__main__":
    main()
