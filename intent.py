#!/usr/bin/env python3
"""
intent.py — explore nav-vs-info signal in BM25 score curves.

Pulls the global top-K queries from /suggest (the same head pool we'll be
generating knowledge-panel summaries for), runs each through /search at
n=10, and looks at the *shape* of the score curve. The intuition is that
a navigational query has one dominant doc — so rank-1 captures most of
the top-10 score mass. An informational query spreads the mass across
many similarly-relevant docs.

The signal we're testing is:
    mass1 = score[0] / sum(score[0..9])

mass1 is bounded [0.1, 1.0]: 0.1 means perfectly flat (all 10 docs
equally relevant), higher means more concentrated on rank-1. We don't
classify yet — this run just reports the histogram of mass1 and prints
sample top-10 score curves so the right threshold can be eyeballed.

Usage:
  python3 intent.py                      # head 2000, n=10 results each
  python3 intent.py --top 500            # smaller pool, faster
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
    """Pull global top-K (query, count) pairs from /suggest."""
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
                c = s["count"]
                if q not in seen or seen[q] < c:
                    seen[q] = c
        except Exception:
            pass
    pairs = sorted(seen.items(), key=lambda x: -x[1])
    return pairs[:top_k]


def fetch_results(base_url: str, query: str, n_results: int) -> list[dict]:
    url = f"{base_url}/search?q={urllib.parse.quote(query)}&n={n_results}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    return data.get("results", [])[:n_results]


def normalise(s: str) -> str:
    return " ".join(s.lower().split())


def docno_to_title(docno: str) -> str:
    return normalise(docno.replace("_", " "))


def histogram(values: list[float], lo: float, hi: float, n_buckets: int = 20) -> str:
    """Render an ASCII histogram with fixed-width bars."""
    if not values:
        return "(no data)"
    width = (hi - lo) / n_buckets
    counts = [0] * n_buckets
    for v in values:
        i = int((v - lo) / width)
        if i < 0:
            i = 0
        elif i >= n_buckets:
            i = n_buckets - 1
        counts[i] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        edge_lo = lo + i * width
        edge_hi = edge_lo + width
        bar = "#" * int(40 * c / max_c)
        lines.append(f"  {edge_lo:5.2f}–{edge_hi:5.2f}  {c:5d}  {bar}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8765", help="base URL of the zettair server")
    parser.add_argument("--top", type=int, default=2000, help="size of the head pool to consider")
    parser.add_argument("--k", type=int, default=10, help="top-K results per query (default 10)")
    parser.add_argument("--n", type=int, default=0, help="number of queries to classify (0 = whole top pool)")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed")
    parser.add_argument("--samples", type=int, default=12, help="curves to print per zone (low / mid / high mass1)")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Fetching top {args.top} queries from {args.url}...", flush=True)
    pairs = fetch_top_queries(args.url, top_k=args.top)
    if not pairs:
        print("No autosuggest entries returned. Is the server running?")
        return
    print(f"Got {len(pairs)} queries (max click count {pairs[0][1]}, min {pairs[-1][1]}).", flush=True)

    queries = [q for q, _ in pairs]
    if args.n and args.n < len(queries):
        weights = [c for _, c in pairs]
        sample = random.choices(queries, weights=weights, k=args.n)
    else:
        sample = queries

    print(f"Fetching top-{args.k} for {len(sample)} queries...", flush=True)
    rows = []
    failures = 0
    failure_examples: list[tuple[str, str]] = []
    for q in sample:
        try:
            res = fetch_results(args.url, q, n_results=args.k)
        except Exception as e:
            failures += 1
            if len(failure_examples) < 8:
                failure_examples.append((q, type(e).__name__ + ": " + str(e)[:80]))
            continue
        scores = [r.get("score", 0.0) for r in res]
        # require K full results; queries with too-few hits are not in the head we care about
        if len(scores) < args.k:
            continue
        if scores[0] <= 0 or sum(scores) <= 0:
            continue
        total = sum(scores)
        mass1 = scores[0] / total
        mass3 = sum(scores[:3]) / total  # top-3 share, useful as a secondary signal
        rank1_doc = docno_to_title(res[0].get("docno", "") or "")
        rows.append({
            "q": q,
            "scores": scores,
            "mass1": mass1,
            "mass3": mass3,
            "ratio12": scores[0] / scores[1] if scores[1] > 0 else float("inf"),
            "rank1": rank1_doc,
        })

    if not rows:
        print("No usable rows.")
        return

    rows.sort(key=lambda r: r["mass1"])

    masses = [r["mass1"] for r in rows]
    print(f"\n=== {len(rows)} queries with full top-{args.k} ({failures} request failures) ===")
    if failure_examples:
        print("first failures:")
        for q, e in failure_examples:
            print(f"  {q!r:<40}  {e}")
    print(
        f"mass1 = score[0] / sum(score[0..{args.k - 1}]):  "
        f"min={min(masses):.3f}  p25={masses[len(masses) // 4]:.3f}  "
        f"median={statistics.median(masses):.3f}  "
        f"p75={masses[3 * len(masses) // 4]:.3f}  max={max(masses):.3f}"
    )
    # mass1 is bounded [1/k, 1.0] — render across that range
    print(f"\nhistogram of mass1 (lower = flatter curve, higher = peakier):")
    print(histogram(masses, lo=1.0 / args.k, hi=1.0, n_buckets=20))

    # Print sample curves from low / mid / high mass1 — eyeball whether the
    # shape matches the intuition before committing to a threshold.
    n = len(rows)
    zones = [
        ("LOW mass1 (flattest curves — likely info)",  rows[: max(1, n // 10)]),
        ("MID mass1 (middle of distribution)",          rows[(n // 2) - args.samples : (n // 2) + args.samples]),
        ("HIGH mass1 (peakiest curves — likely nav)",   rows[-max(1, n // 10) :]),
    ]
    for label, zone in zones:
        print(f"\n--- {label} ---")
        random.shuffle(zone)
        for r in zone[: args.samples]:
            curve = " ".join(f"{s:5.2f}" for s in r["scores"])
            print(
                f"  mass1={r['mass1']:.3f}  mass3={r['mass3']:.3f}  "
                f"r1/r2={r['ratio12']:5.2f}  q={r['q']!r:<35}  rank1={r['rank1']!r}"
            )
            print(f"    curve: {curve}")


if __name__ == "__main__":
    main()
