#!/usr/bin/env python3
"""
summaries_admin.py — Build / inspect the PRD-018 knowledge-panel summary store.

Two FlatStore-format files live next to the other sidecars:
  summaries.store  — concatenated UTF-8 summary markdown blobs
  summaries.map    — JSON dict {query_norm: [offset, length]}

This script is the source-of-truth tool for offline summary management
while the model-driven pipeline is being built. Once generate_summaries.py
(M4) exists, it'll be replaced by a real install step. For now, it
supports:

  build    — read a JSONL file ({"query": ..., "summary_md": ...}) and
             write a complete store + map. Overwrites destination atomically.
  add      — append/update a single (query, summary) pair. Loads existing
             store, appends new blob, rewrites map.
  list     — show every (query, length, preview) in the store.
  get      — print the summary markdown for one query.
  delete   — remove a query from the map (the .store blob is left in
             place; storage reclaim happens on the next `build`).

All commands accept --store and --map paths; defaults are relative
to the current directory.

Usage:
  python3 tools/summaries_admin.py build --in summaries.jsonl --store summaries.store --map summaries.map
  python3 tools/summaries_admin.py add "morrissey" "**Morrissey** is..."
  python3 tools/summaries_admin.py list
  python3 tools/summaries_admin.py get "morrissey"
  python3 tools/summaries_admin.py delete "morrissey"
"""

import argparse
import json
import os
import sys


def query_norm(s: str) -> str:
    """Must stay identical to server.py:query_norm."""
    return " ".join(s.lower().strip().split())


def load_map(map_path: str) -> dict:
    if not os.path.exists(map_path):
        return {}
    with open(map_path, encoding="utf-8") as f:
        return json.load(f)


def write_map(map_path: str, m: dict) -> None:
    tmp = map_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, map_path)


def cmd_build(args: argparse.Namespace) -> None:
    entries = []
    with open(args.input, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            q = rec.get("query") or rec.get("query_norm")
            md = rec.get("summary_md") or rec.get("summary")
            if not q or not md:
                print(f"  warn: line {line_no} missing query/summary_md", file=sys.stderr)
                continue
            entries.append((query_norm(q), md))

    new_map: dict = {}
    tmp_store = args.store + ".tmp"
    tmp_map = args.map + ".tmp"
    offset = 0
    with open(tmp_store, "wb") as store:
        for q, md in entries:
            blob = md.encode("utf-8")
            new_map[q] = [offset, len(blob)]
            store.write(blob)
            offset += len(blob)
    write_map(tmp_map, new_map)
    os.replace(tmp_store, args.store)
    os.replace(tmp_map, args.map)
    print(f"Wrote {len(new_map):,} summaries to {args.store} (+ {args.map})")


def cmd_add(args: argparse.Namespace) -> None:
    q = query_norm(args.query)
    md = args.summary
    existing_map = load_map(args.map)
    blob = md.encode("utf-8")
    if os.path.exists(args.store):
        with open(args.store, "ab") as store:
            offset = store.tell()
            store.write(blob)
    else:
        with open(args.store, "wb") as store:
            offset = 0
            store.write(blob)
    existing_map[q] = [offset, len(blob)]
    write_map(args.map, existing_map)
    print(f"Added {q!r} → offset={offset} len={len(blob)}")


def cmd_list(args: argparse.Namespace) -> None:
    m = load_map(args.map)
    if not m:
        print("(empty)")
        return
    fd = os.open(args.store, os.O_RDONLY)
    try:
        for q in sorted(m):
            offset, length = m[q]
            blob = os.pread(fd, length, offset).decode("utf-8", errors="replace")
            preview = blob.replace("\n", " ")[:60]
            print(f"  {q:<35}  {length:>6}B  {preview}{'…' if len(blob) > 60 else ''}")
    finally:
        os.close(fd)
    print(f"\n{len(m)} entries total")


def cmd_get(args: argparse.Namespace) -> None:
    q = query_norm(args.query)
    m = load_map(args.map)
    if q not in m:
        print(f"(no summary for {q!r})", file=sys.stderr)
        sys.exit(1)
    offset, length = m[q]
    fd = os.open(args.store, os.O_RDONLY)
    try:
        blob = os.pread(fd, length, offset).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    sys.stdout.write(blob)
    if not blob.endswith("\n"):
        sys.stdout.write("\n")


def cmd_delete(args: argparse.Namespace) -> None:
    q = query_norm(args.query)
    m = load_map(args.map)
    if q in m:
        del m[q]
        write_map(args.map, m)
        print(f"Removed {q!r} from the map. Run `build` to reclaim its bytes in the .store.")
    else:
        print(f"(no entry for {q!r})", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", default="summaries.store", help="path to summaries.store")
    parser.add_argument("--map",   default="summaries.map",   help="path to summaries.map")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="rebuild store+map from a JSONL file")
    p_build.add_argument("--in", dest="input", required=True, help="JSONL with {query, summary_md} per line")
    p_build.set_defaults(func=cmd_build)

    p_add = sub.add_parser("add", help="add or update one (query, summary) pair")
    p_add.add_argument("query")
    p_add.add_argument("summary")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list every entry with a short preview")
    p_list.set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", help="print the summary markdown for a query")
    p_get.add_argument("query")
    p_get.set_defaults(func=cmd_get)

    p_del = sub.add_parser("delete", help="remove a query from the map")
    p_del.add_argument("query")
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
