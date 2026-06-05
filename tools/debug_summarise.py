#!/usr/bin/env python3
"""
debug_summarise.py — Diagnose what summarise.py picks for a given
docno + query against the live docstore.

Reads the docstore directly via the same FlatStore pattern the server
uses, runs split_fragments + _score_and_check on every fragment,
prints position / length / density / positional-boost / post-boost
score, then shows the top SHOW_FRAGS that would be picked.

Designed for one-shot diagnosis on prod when a snippet looks wrong:

  sudo -u zettair python3 /opt/zettair-search/tools/debug_summarise.py \\
      --docno Vladimir_Putin --query "vladimir putin"

Reads env vars ZET_DOCSTORE and ZET_DOCMAP (same as the server unit).
"""

import argparse
import json
import math
import os
import sys

# Make summarise importable when running from tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import summarise  # noqa: E402


def _load_docstore_text(docno: str) -> str:
    docstore_path = os.environ.get("ZET_DOCSTORE")
    docmap_path   = os.environ.get("ZET_DOCMAP")
    if not docstore_path or not docmap_path:
        sys.exit("ERROR: set ZET_DOCSTORE and ZET_DOCMAP (same as the systemd unit)")
    if not os.path.exists(docstore_path) or not os.path.exists(docmap_path):
        sys.exit(f"ERROR: missing files: {docstore_path} or {docmap_path}")
    with open(docmap_path, encoding="utf-8") as f:
        docmap = json.load(f)
    entry = docmap.get(docno)
    if entry is None:
        sys.exit(f"ERROR: docno {docno!r} not in docmap")
    offset, length = entry
    fd = os.open(docstore_path, os.O_RDONLY)
    try:
        return os.pread(fd, length, offset).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--docno", required=True, help="e.g. Vladimir_Putin")
    p.add_argument("--query", required=True, help='e.g. "vladimir putin"')
    p.add_argument("--top", type=int, default=15,
                   help="show top-N fragments by post-boost score (default 15)")
    p.add_argument("--prefix", type=int, default=110,
                   help="chars of fragment text to print (default 110)")
    args = p.parse_args()

    text = _load_docstore_text(args.docno)
    print(f"docno: {args.docno}")
    print(f"docstore bytes: {len(text):,}")
    print(f"first paragraph break at byte: "
          f"{text.find(chr(10) + chr(10))}  (negative = no \\n\\n found)")
    print()

    terms = summarise.parse_query(args.query)
    print(f"query terms: {sorted(terms)}")
    print(f"LEAD_WEIGHT: {summarise.LEAD_WEIGHT}")
    print(f"SHOW_FRAGS:  {summarise.SHOW_FRAGS}")
    print(f"MIN_FRAG_CHARS: {summarise.MIN_FRAG_CHARS}")
    print(f"MAX_FRAG_SCORE_CHARS: {summarise.MAX_FRAG_SCORE_CHARS}")
    print()

    frags = summarise.split_fragments(text)
    print(f"total fragments: {len(frags)}")
    print()

    rows = []
    for i, f in enumerate(frags):
        density = summarise._score_and_check(f, terms)
        boost = 1.0 + summarise.LEAD_WEIGHT / math.log2(2 + i)
        post = density * boost
        rows.append((i, len(f), density, boost, post, f))

    # Filter to those with non-zero density (the ones eligible for selection).
    eligible = [r for r in rows if r[2] > 0.0]
    print(f"fragments with hits: {len(eligible)}")
    print()

    # Sort by post-boost score (what summarise_doc picks from), show top N.
    eligible.sort(key=lambda r: -r[4])
    print(f"top {args.top} eligible fragments by POST-boost score:")
    print(f"{'pos':>5}  {'len':>5}  {'density':>8}  {'boost':>6}  "
          f"{'post':>8}  prefix")
    for i, length, density, boost, post, f in eligible[:args.top]:
        prefix = f[:args.prefix].replace("\n", " ")
        print(f"{i:>5}  {length:>5}  {density:>8.4f}  {boost:>6.3f}  "
              f"{post:>8.4f}  {prefix!r}")
    print()

    # What summarise_doc would actually pick (top SHOW_FRAGS, then re-ordered
    # by position for display).
    picked = sorted(eligible, key=lambda r: -r[4])[:summarise.SHOW_FRAGS]
    picked.sort(key=lambda r: r[0])
    print(f"would pick (top {summarise.SHOW_FRAGS} by score, re-ordered by position):")
    for i, length, density, boost, post, f in picked:
        prefix = f[:args.prefix].replace("\n", " ")
        print(f"  pos={i} post={post:.4f}: {prefix!r}")
    print()

    # Also: where IS the lede? Print the first 5 fragments verbatim, so
    # we can see whether fragment 0 is real lede prose or something else.
    print("first 5 fragments verbatim (the lede should be one of these):")
    for i, f in enumerate(frags[:5]):
        prefix = f[:args.prefix * 2].replace("\n", " ")
        density = summarise._score_and_check(f, terms)
        print(f"  [{i}] len={len(f)} density={density:.4f}: {prefix!r}")


if __name__ == "__main__":
    main()
