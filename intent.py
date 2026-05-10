#!/usr/bin/env python3
"""
intent.py — explore nav-vs-info signal in BM25 score curves.

Pulls the global top-K queries from /suggest, runs each through /search
at n=10, and looks at the *shape* of the top-K score curve. Idea: nav
queries have one dominant doc; info queries spread similar scores
across many docs.

The primary signal is:
    head_floor_ratio = score[0] / score[k-1]

i.e. how much does the head of the curve dominate its floor. Compared
to mass1 (rank-1 share of total) this preserves dynamic range — mass1
is bounded [1/k, 1.0] and on real data collapses into 0.10–0.18, which
is too narrow to threshold cleanly. r1/r_k uses a multiplicative scale
that's not crushed by absolute BM25 levels.

mass1 is still recorded per row for reference.

Usage:
  python3 intent.py                      # head 2000, k=10
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
        if scores[0] <= 0 or scores[-1] <= 0:
            continue
        total = sum(scores)
        mass1 = scores[0] / total
        head_floor = scores[0] / scores[-1]   # primary signal
        rank1_doc = docno_to_title(res[0].get("docno", "") or "")
        rows.append({
            "q": q,
            "scores": scores,
            "mass1": mass1,
            "head_floor": head_floor,
            "ratio12": scores[0] / scores[1] if scores[1] > 0 else float("inf"),
            "rank1": rank1_doc,
        })

    if not rows:
        print("No usable rows.")
        return

    rows.sort(key=lambda r: r["head_floor"])

    hf = [r["head_floor"] for r in rows]
    print(f"\n=== {len(rows)} queries with full top-{args.k} ({failures} request failures) ===")
    if failure_examples:
        print("first failures:")
        for q, e in failure_examples:
            print(f"  {q!r:<40}  {e}")
    print(
        f"head_floor = score[0] / score[{args.k - 1}]:  "
        f"min={min(hf):.2f}  p25={hf[len(hf) // 4]:.2f}  "
        f"median={statistics.median(hf):.2f}  "
        f"p75={hf[3 * len(hf) // 4]:.2f}  p95={hf[int(0.95 * len(hf))]:.2f}  max={max(hf):.2f}"
    )
    # head_floor is bounded [1.0, +inf]. Pick a sensible upper bound for the
    # histogram from the data — clamp at p99 so a single outlier doesn't
    # squash the bulk into one bucket.
    hi = hf[int(0.99 * len(hf))]
    print(f"\nhistogram of head_floor (1.0 = flat, larger = peakier; clamped to p99={hi:.2f}):")
    print(histogram(hf, lo=1.0, hi=hi, n_buckets=20))

    # Print sample curves from low / mid / high zones — eyeball whether
    # the shape matches the intuition before committing to a threshold.
    n = len(rows)
    zones = [
        ("LOW head_floor (flattest — likely info or broken)",  rows[: max(1, n // 10)]),
        ("MID head_floor (middle of distribution)",            rows[(n // 2) - args.samples : (n // 2) + args.samples]),
        ("HIGH head_floor (peakiest — likely nav)",            rows[-max(1, n // 10) :]),
    ]
    for label, zone in zones:
        print(f"\n--- {label} ---")
        random.shuffle(zone)
        for r in zone[: args.samples]:
            curve = " ".join(f"{s:5.2f}" for s in r["scores"])
            print(
                f"  hf={r['head_floor']:5.2f}  mass1={r['mass1']:.3f}  "
                f"r1/r2={r['ratio12']:5.2f}  q={r['q']!r:<35}  rank1={r['rank1']!r}"
            )
            print(f"    curve: {curve}")


if __name__ == "__main__":
    main()
