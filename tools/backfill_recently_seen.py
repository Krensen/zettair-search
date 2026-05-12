#!/usr/bin/env python3
"""One-shot: seed recently_seen.json from installed/:news mtimes.

After PRD-021's grace-window feature deployed, the grace-window only
protects queries spotted on the rail AFTER deploy. Anyone who dropped
off in the gap (e.g. Elon Musk on 2026-05-12) had no record. This
script reconstructs recently_seen entries by treating "this :news.md
was installed in the last GRACE_DAYS" as a proxy for "this query was
recently spiking". Safe to run once; harmless if re-run.
"""
from pathlib import Path
import datetime as dt
import json
import os

INSTALLED = Path("/mnt/wikipedia-source/summaries/installed")
RS_PATH   = Path("/mnt/wikipedia-source/trending/recently_seen.json")
GRACE_DAYS = 7   # match SERVE_GRACE_DAYS

now = dt.datetime.now(dt.UTC)
cutoff = now - dt.timedelta(days=GRACE_DAYS)

seen = {}
if RS_PATH.exists():
    try:
        seen = json.loads(RS_PATH.read_text())
    except Exception:
        seen = {}

added = 0
for md in INSTALLED.glob("*:news.md"):
    mtime = dt.datetime.fromtimestamp(md.stat().st_mtime, tz=dt.UTC)
    if mtime < cutoff:
        continue
    qnorm = md.name[:-len(":news.md")]
    if qnorm in seen:
        continue   # don't overwrite a fresher live entry
    seen[qnorm] = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
    added += 1
    print(f"  + {qnorm}  (installed {mtime})")

RS_PATH.parent.mkdir(parents=True, exist_ok=True)
tmp = RS_PATH.with_suffix(".json.tmp")
tmp.write_text(json.dumps(seen, separators=(",", ":"), sort_keys=True))
os.replace(tmp, RS_PATH)
print(f"backfilled {added} entries into {RS_PATH}; total now {len(seen)}")
