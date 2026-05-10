#!/usr/bin/env python3
"""
intent.py — decide which head queries should get a knowledge-panel summary.

Pulls the global top-K queries from /suggest, runs each through /search at
n=10, and uses the shape of the top-10 BM25 score curve to decide whether
a knowledge-panel summary is worth generating for that query.

The decision is single-threshold:

    head_floor = score[0] / score[k-1]
    skip_summary = head_floor >= --skip-threshold   (default 1.50)

Idea: when the rank-1 score dominates the floor of the top-10 by a big
margin, the top result IS the answer and a knowledge-panel summary
adds nothing. Everything else gets a summary. That includes the "broad
topic" queries (flat curves) and the merely-ambiguous middle.

This run reports the threshold, the histogram, and writes the full
summary-worthy queries list to a file so you can eyeball coverage and
sanity-check the call.

Usage:
  python3 intent.py                                # head 2000, k=10, threshold 1.50
  python3 intent.py --skip-threshold 1.30          # tighter — skip more queries
  python3 intent.py --out summary_queries.txt
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
    parser.add_argument("--skip-threshold", type=float, default=1.50,
                        help="head_floor at or above this means skip summary (top result IS the answer)")
    parser.add_argument("--out", default="summary_queries.txt",
                        help="where to write the summary-worthy queries list")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Fetching top {args.top} queries from {args.url}...", flush=True)
    pairs = fetch_top_queries(args.url, top_k=args.top)
    if not pairs:
        print("No autosuggest entries returned. Is the server running?")
        return
    click_by_q = dict(pairs)
    print(f"Got {len(pairs)} queries (max click count {pairs[0][1]}, min {pairs[-1][1]}).", flush=True)

    queries = [q for q, _ in pairs]

    print(f"Fetching top-{args.k} for {len(queries)} queries...", flush=True)
    rows = []
    failures = 0
    failure_examples: list[tuple[str, str]] = []
    for q in queries:
        try:
            res = fetch_results(args.url, q, args.k)
        except Exception as e:
            failures += 1
            if len(failure_examples) < 8:
                failure_examples.append((q, type(e).__name__ + ": " + str(e)[:80]))
            continue
        scores = [r.get("score", 0.0) for r in res]
        if len(scores) < args.k or scores[0] <= 0 or scores[-1] <= 0:
            continue
        rows.append({
            "q": q,
            "scores": scores,
            "head_floor": scores[0] / scores[-1],
            "rank1": docno_to_title(res[0].get("docno", "") or ""),
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
        f"head_floor:  min={min(hf):.2f}  p25={hf[len(hf) // 4]:.2f}  "
        f"median={statistics.median(hf):.2f}  p75={hf[3 * len(hf) // 4]:.2f}  "
        f"p95={hf[int(0.95 * len(hf))]:.2f}  max={max(hf):.2f}"
    )
    hi = hf[int(0.99 * len(hf))]
    print(f"\nhistogram of head_floor (1.0 = flat, larger = peakier; clamped to p99={hi:.2f}):")
    print(histogram(hf, lo=1.0, hi=hi, n_buckets=20))

    skip = [r for r in rows if r["head_floor"] >= args.skip_threshold]
    keep = [r for r in rows if r["head_floor"] < args.skip_threshold]
    print(f"\nat skip-threshold {args.skip_threshold}:")
    print(f"  skip summary (top is the answer):     {len(skip):5d}  ({100.0 * len(skip) / len(rows):.1f}%)")
    print(f"  generate summary (informational-ish): {len(keep):5d}  ({100.0 * len(keep) / len(rows):.1f}%)")

    print(f"\n=== sample of 10 queries we'd SKIP (top is the answer) ===")
    for r in random.sample(skip, min(10, len(skip))):
        print(f"  hf={r['head_floor']:5.2f}  q={r['q']!r:<40}  rank1={r['rank1']!r}")

    # Write the full summary-worthy list to a file, sorted by click count
    # (most popular first — that's the natural processing order for the
    # knowledge-panel job).
    keep.sort(key=lambda r: -click_by_q.get(r["q"], 0))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# {len(keep)} queries marked summary-worthy (head_floor < {args.skip_threshold})\n")
        f.write(f"# Sorted by click count (most popular first).\n")
        f.write(f"# columns: clicks  head_floor  query  -> rank1_doc\n")
        for r in keep:
            clicks = click_by_q.get(r["q"], 0)
            f.write(f"{clicks:>10}  {r['head_floor']:5.2f}  {r['q']!r:<40}  -> {r['rank1']!r}\n")
    print(f"\nWrote {len(keep)} summary-worthy queries to {args.out}")
    print(f"  (sorted by click count, most popular first; head: {keep[0]['q']!r})")


if __name__ == "__main__":
    main()
