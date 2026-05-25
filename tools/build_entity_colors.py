#!/usr/bin/env python3
"""PRD-029: assign stable palette slots to recurring entities.

Reads trending/events.jsonl and selects every docno that has appeared
on at least MIN_EVENTS distinct days within the past LOOKBACK_DAYS.
Assigns each qualifying docno to a palette slot (1..16). Assignments
are sticky — once an entity has a slot, it keeps it until manually
edited.

Outputs trending/events.entity_colors.json:

    {
      "version": 1,
      "lookback_days": 60,
      "min_events": 3,
      "min_distinct_days": 2,
      "colors": {"Iran": 1, "Ukraine": 2, ...},
      "next_slot_pointer": 5,
      "built_at": "ISO8601"
    }

The palette itself lives in news.html as CSS custom properties
(--ent-1 through --ent-16). This script only picks slots.

Design choices:

- 16 slots only. More slots dilutes the visual-memory effect — if a
  user sees too many different colours they cannot remember which
  is which. Recurring entities beyond the 16th are bucketed into
  the neutral default colour.
- Sticky assignments. If we shuffled slots on every rebuild, the
  visual through-line (Iran red across multiple days/weeks) would
  break every day.
- "Recurring" = appears on >= MIN_DISTINCT_DAYS distinct days with
  >= MIN_EVENTS total events. One-shot spikes do not earn a slot.
- New entities get the slot with the fewest current assignees.
- Manual overrides: edit events.entity_colors.json by hand; this
  script preserves existing assignments and only adds new ones.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

PALETTE_SLOTS    = 16
LOOKBACK_DAYS    = 60
MIN_EVENTS       = 3
MIN_DISTINCT_DAYS = 2

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "colors": {}, "next_slot_pointer": 1}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Tolerate either {colors: {...}} or flat {docno: slot}.
        if "colors" not in data and isinstance(data, dict):
            data = {"version": 1, "colors": data, "next_slot_pointer": 1}
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "colors": {}, "next_slot_pointer": 1}


def count_recurrence(journal_path: Path, today: dt.date,
                     lookback_days: int) -> dict[str, tuple[int, set[str]]]:
    """Return {docno: (total_events, set_of_distinct_dates)}."""
    floor = (today - dt.timedelta(days=lookback_days)).isoformat()
    counts: dict[str, tuple[int, set[str]]] = defaultdict(lambda: (0, set()))
    if not journal_path.exists():
        return {}
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ev_date = r.get("event_date") or ""
            if ev_date < floor:
                continue
            docno = r.get("docno")
            if not docno:
                continue
            total, dates = counts[docno]
            dates.add(ev_date)
            counts[docno] = (total + 1, dates)
    return counts


def pick_slot(existing_colors: dict[str, int], next_pointer: int) -> int:
    """Return the slot index (1..PALETTE_SLOTS) with the fewest current
    assignees. Ties broken by ascending slot number. Round-robins
    through fresh slots first when none have hit the cap yet."""
    usage = {i: 0 for i in range(1, PALETTE_SLOTS + 1)}
    for slot in existing_colors.values():
        if 1 <= slot <= PALETTE_SLOTS:
            usage[slot] += 1
    # Prefer empty slots in pointer order, then least-used.
    return min(range(1, PALETTE_SLOTS + 1),
               key=lambda i: (usage[i], (i - next_pointer) % PALETTE_SLOTS))


def build(journal_path: Path, out_path: Path,
          today: dt.date | None = None,
          lookback_days: int = LOOKBACK_DAYS,
          min_events: int = MIN_EVENTS,
          min_distinct_days: int = MIN_DISTINCT_DAYS) -> None:
    today = today or dt.datetime.now(dt.UTC).date()
    print(f"reading journal: {journal_path}", flush=True)
    counts = count_recurrence(journal_path, today, lookback_days)
    print(f"  {len(counts):,} distinct docnos in the last {lookback_days} days",
          flush=True)

    qualifying = [
        (docno, total, dates)
        for docno, (total, dates) in counts.items()
        if total >= min_events and len(dates) >= min_distinct_days
    ]
    qualifying.sort(key=lambda x: (-x[1], -len(x[2]), x[0]))
    print(f"  {len(qualifying):,} qualify "
          f"(>= {min_events} events on >= {min_distinct_days} days)",
          flush=True)

    existing = load_existing(out_path)
    colors: dict[str, int] = dict(existing.get("colors", {}))
    pointer: int = int(existing.get("next_slot_pointer", 1))

    added = 0
    for docno, total, dates in qualifying:
        if docno in colors:
            continue   # sticky assignment
        slot = pick_slot(colors, pointer)
        colors[docno] = slot
        pointer = (slot % PALETTE_SLOTS) + 1
        added += 1
        if added <= 10:
            print(f"    + slot {slot}: {docno} "
                  f"({total} events on {len(dates)} days)", flush=True)
    if added > 10:
        print(f"    ... and {added - 10} more", flush=True)

    payload = {
        "version": 1,
        "lookback_days": lookback_days,
        "min_events": min_events,
        "min_distinct_days": min_distinct_days,
        "palette_size": PALETTE_SLOTS,
        "colors": colors,
        "next_slot_pointer": pointer,
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, out_path)
    print(f"wrote {out_path}: {len(colors):,} assignments total "
          f"({added} new this run)", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--output", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.entity_colors.json")
    p.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    p.add_argument("--min-events",    type=int, default=MIN_EVENTS)
    p.add_argument("--min-distinct-days", type=int, default=MIN_DISTINCT_DAYS)
    args = p.parse_args()
    if not args.journal.exists():
        print(f"ERROR: journal not found at {args.journal}", file=sys.stderr)
        return 1
    build(args.journal, args.output,
          lookback_days=args.lookback_days,
          min_events=args.min_events,
          min_distinct_days=args.min_distinct_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
