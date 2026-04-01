#!/usr/bin/env python3
"""
Daily query digest — reads logs/queries.jsonl and sends a Telegram summary.
Run via cron or openclaw scheduler.
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "queries.jsonl")

# Queries to exclude from the digest (health checks, test queries, etc.)
DIGEST_BLOCKLIST = {"test"}

def load_queries(since_hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    queries = []
    if not os.path.exists(LOG_PATH):
        return queries
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
                if ts >= cutoff:
                    q = rec["q"].lower().strip()
                    if q not in DIGEST_BLOCKLIST:
                        queries.append(q)
            except Exception:
                continue
    return queries

def build_digest(since_hours=24):
    queries = load_queries(since_hours)
    if not queries:
        return None
    counts = Counter(queries).most_common()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"🔍 *Zettair daily digest — {date_str}*", f"_{len(queries)} searches, {len(counts)} unique_", ""]
    for q, n in counts:
        bar = "▪" * min(n, 10)
        lines.append(f"{bar} `{q}` — {n}x" if n > 1 else f"▪ `{q}`")
    return "\n".join(lines)

if __name__ == "__main__":
    digest = build_digest()
    if digest:
        print(digest)
    else:
        print("No queries in the last 24 hours.")
