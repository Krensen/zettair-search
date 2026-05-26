#!/usr/bin/env python3
"""PRD-029: build the news-timeline event index from the trending journal.

Reads /mnt/wikipedia-source/trending/events.jsonl (the append-only
journal written by fetch_trending.py), de-duplicates by
(docno, event_date) keeping the latest captured_at, joins to the
images store + entity_class.json for visual data + classification,
and emits two output files:

    events.jsonl   — one record per (docno, event_date), sorted by
                     event_date DESC, then rank_hint DESC, then docno
                     ASC. UTF-8, separators=(",",":"). Each line is a
                     stand-alone JSON object the server can hand to
                     the frontend without further enrichment.

    events.idx     — per-date byte-offset map. JSON object:
                       {"version": 1,
                        "event_date_index": {"YYYY-MM-DD": [start, length], ...},
                        "total_events": N,
                        "built_at": "ISO8601"}
                     Server loads at startup; range reads by date use
                     os.pread on events.jsonl for the (start, length)
                     slice.

Idempotent. Safe to re-run any time — it derives entirely from the
journal + image store + entity-class file.

The entity-colour map is built by a separate script
(build_entity_colors.py); this one writes only the events themselves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path


# Inline mirror of fetch_trending.title_to_query — kept here to avoid
# importing the whole trending module. Must stay in sync.
_DISAMB_PAREN = re.compile(r"_\([^()]+\)$")
_TRAILING_PUNCT = re.compile(r"[\.,!?]+$")


def _query_norm_from_docno(docno: str) -> str:
    t = _DISAMB_PAREN.sub("", docno)
    t = t.replace("_", " ")
    t = urllib.parse.unquote(t)
    t = _TRAILING_PUNCT.sub("", t)
    t = " ".join(t.split())
    return t.lower()

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))
DEFAULT_VOLUME = Path(os.environ.get(
    "ZET_VOLUME", "/mnt/wikipedia-source",
))


# ---------------------------------------------------------------------------
# Read-only FlatStore — same shape as the one in build_summary_jobs.py /
# build_reading_sidecar.py. We need the images store for image_url joins.
# ---------------------------------------------------------------------------

class FlatStoreRO:
    def __init__(self, store_path: Path, map_path: Path):
        self.store_path = store_path
        self.map_path = map_path
        self._map: dict = {}
        self._fd: int = -1
        self._ok = False

    def load(self) -> bool:
        if not self.map_path.exists() or not self.store_path.exists():
            return False
        with open(self.map_path, encoding="utf-8") as f:
            self._map = json.load(f)
        self._fd = os.open(self.store_path, os.O_RDONLY)
        self._ok = True
        return True

    def get(self, key: str) -> str | None:
        if not self._ok:
            return None
        entry = self._map.get(key)
        if entry is None:
            return None
        offset, length = entry
        try:
            return os.pread(self._fd, length, offset).decode("utf-8", errors="replace")
        except OSError:
            return None

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------

def iter_journal(path: Path):
    """Yield records from events.jsonl. Skips malformed lines."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                # Corrupt line; log to stderr but keep going.
                print(f"  skipping malformed line {line_no}", file=sys.stderr)


def dedupe_keep_latest(records):
    """Collapse records on (docno, event_date), keeping the latest
    captured_at. Returns a list of dicts."""
    latest: dict[tuple[str, str], dict] = {}
    for r in records:
        docno = r.get("docno")
        ev_date = r.get("event_date")
        if not docno or not ev_date:
            continue
        key = (docno, ev_date)
        prev = latest.get(key)
        if prev is None or (r.get("t", "") > prev.get("t", "")):
            latest[key] = r
    return list(latest.values())


def compute_rank_hint(rec: dict, class_weights: dict[str, float]) -> float:
    """Simple weighted score: trending-source priority + entity class
    weight + source-rank decay. Higher = more important.

    This is intentionally a starter heuristic; tuning happens after
    the first ~30 days of timeline use."""
    # trending_source priority: google_news > spike > wiki_itn > unknown
    src_score = {"google_news": 3.0, "spike": 2.0, "wiki_itn": 1.5}.get(
        rec.get("trending_source") or "", 1.0
    )
    # source_rank decay (rank 1 is best). 1/(rank^0.5) gives a gentle
    # falloff: rank 1 -> 1.0, rank 4 -> 0.5, rank 16 -> 0.25.
    sr = rec.get("source_rank")
    rank_score = 1.0 / max(int(sr), 1) ** 0.5 if isinstance(sr, (int, float)) else 0.5
    # Entity-class weight.
    cls = rec.get("entity_class") or "unknown"
    cls_weight = class_weights.get(cls, 1.0)
    return src_score * rank_score * cls_weight


def build(journal_path: Path,
          images_store: Path, images_map: Path,
          entity_class_path: Path,
          out_dir: Path,
          categories_path: Path | None = None,
          summaries_store: Path | None = None,
          summaries_map: Path | None = None,
          day_summaries_journal: Path | None = None,
          today: dt.date | None = None,
          horizon_days: int = 90,
          class_weights: dict[str, float] | None = None) -> None:
    print(f"reading journal: {journal_path}", flush=True)
    records = list(iter_journal(journal_path))
    print(f"  {len(records):,} raw records", flush=True)

    deduped = dedupe_keep_latest(records)
    print(f"  {len(deduped):,} unique (docno, event_date) pairs", flush=True)

    # Filter to events in the past horizon_days. Older events stay in
    # the journal but don't get rebuilt into the timeline until the
    # year-zoom feature ships in v1.5.
    today = today or dt.datetime.now(dt.UTC).date()
    floor_date = (today - dt.timedelta(days=horizon_days)).isoformat()
    fresh = [r for r in deduped if (r.get("event_date") or "") >= floor_date]
    print(f"  {len(fresh):,} within last {horizon_days} days "
          f"(>= {floor_date})", flush=True)

    # Join: image_url + entity_class.
    print(f"loading images store: {images_store}", flush=True)
    images = FlatStoreRO(images_store, images_map)
    images_ok = images.load()
    if not images_ok:
        print("  WARN: images store unavailable; image_url omitted", flush=True)

    entity_classes: dict[str, str] = {}
    if entity_class_path.exists():
        print(f"loading entity classes: {entity_class_path}", flush=True)
        try:
            with open(entity_class_path, encoding="utf-8") as f:
                entity_classes = json.load(f)
            print(f"  {len(entity_classes):,} classifications", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN: couldn't load entity-class file: {e}", flush=True)
    else:
        print(f"  WARN: {entity_class_path} not found; entity_class omitted",
              flush=True)

    class_weights = class_weights or {
        "place":        1.10,
        "event":        1.10,
        "organisation": 1.05,
        "human":        1.00,
        "work":         0.85,
        "unknown":      0.90,
    }

    # PRD-029 step 2: category sidecar join, if present.
    categories: dict[str, str] = {}
    if categories_path and categories_path.exists():
        print(f"loading categories: {categories_path}", flush=True)
        try:
            with open(categories_path, encoding="utf-8") as f:
                cat_payload = json.load(f)
            categories = cat_payload.get("categories") or {}
            print(f"  {len(categories):,} category assignments", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN: couldn't load categories: {e}", flush=True)

    # PRD-029 step 3: LLM summary join. Two key shapes considered:
    #   1. <docno>:<event_date>:event  — purpose-built per-event
    #      summary (only present if we explicitly enqueued one).
    #   2. <query_norm>:news            — the PRD-021 search-panel
    #      summary; reused on the timeline when the event is recent
    #      enough that it's plausibly the same incident, so the
    #      timeline and the search knowledge-panel agree on the text.
    # NEWS_REUSE_DAYS bounds the reuse window so a March IAEA summary
    # does not get attached to a January Iran event.
    NEWS_REUSE_DAYS = 14
    today_iso = today.isoformat() if today else dt.date.today().isoformat()
    news_floor_iso = (dt.date.fromisoformat(today_iso) -
                      dt.timedelta(days=NEWS_REUSE_DAYS)).isoformat()

    summaries = None
    event_keys: set[str] = set()
    news_keys: set[str] = set()
    if summaries_store and summaries_map and summaries_store.exists() and summaries_map.exists():
        print(f"loading summaries store: {summaries_store}", flush=True)
        summaries = FlatStoreRO(summaries_store, summaries_map)
        if summaries.load():
            for k in summaries._map.keys():
                if k.endswith(":event"):
                    event_keys.add(k)
                elif k.endswith(":news"):
                    news_keys.add(k)
            print(f"  {len(event_keys):,} event-summary keys, "
                  f"{len(news_keys):,} :news-summary keys",
                  flush=True)
        else:
            summaries = None

    # Enrich each record.
    n_with_event_summary = 0
    n_with_news_summary  = 0
    for r in fresh:
        docno = r["docno"]
        if images_ok:
            img = images.get(docno)
            if img:
                r["image_url"] = img
        cls = entity_classes.get(docno)
        if cls:
            r["entity_class"] = cls
        cat = categories.get(f"{docno}:{r.get('event_date', '')}")
        if cat:
            r["category"] = cat
        if summaries is not None:
            ev_date = r.get("event_date", "")
            # Primary: per-event summary.
            ek = f"{docno}:{ev_date}:event"
            body = None
            if ek in event_keys:
                body = summaries.get(ek)
                if body:
                    r["summary_md"] = body
                    r["summary_source"] = "event"
                    n_with_event_summary += 1
            # Fallback: PRD-021 :news summary, only for events within
            # NEWS_REUSE_DAYS of today (so it is plausibly the same
            # incident the search panel is showing).
            if body is None and ev_date >= news_floor_iso:
                qn = _query_norm_from_docno(docno)
                nk = f"{qn}:news"
                if nk in news_keys:
                    body = summaries.get(nk)
                    if body:
                        r["summary_md"] = body
                        r["summary_source"] = "news"
                        n_with_news_summary += 1
        r["rank_hint"] = round(compute_rank_hint(r, class_weights), 4)
    if summaries is not None:
        summaries.close()
        print(f"  joined LLM summary onto {n_with_event_summary:,} events "
              f"(:event) + {n_with_news_summary:,} (:news reused)",
              flush=True)

    images.close()

    # Sort: event_date DESC, rank_hint DESC, docno ASC.
    fresh.sort(key=lambda r: (
        r.get("event_date") or "",
        r.get("rank_hint", 0),
        # docno is asc — invert for the sort by descending all then
        # reversing… cleaner: keyed by negative numeric fields.
    ), reverse=True)
    # Secondary stable tie-break on docno asc within (date, rank_hint).
    fresh.sort(key=lambda r: r.get("docno") or "")
    fresh.sort(key=lambda r: (r.get("event_date") or "", r.get("rank_hint", 0)),
               reverse=True)

    # Write events.jsonl + events.idx atomically.
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    idx_path    = out_dir / "events.idx"
    events_tmp  = events_path.with_suffix(".jsonl.tmp")
    idx_tmp     = idx_path.with_suffix(".idx.tmp")

    # The index maps event_date -> [start_byte, length_bytes] in
    # events.jsonl. Build it by streaming writes and watching offsets.
    date_index: dict[str, list[int]] = {}
    with open(events_tmp, "w", encoding="utf-8") as f:
        # Group records by event_date and emit each group together so a
        # date lookup is one os.pread range read.
        by_date: dict[str, list[dict]] = {}
        for r in fresh:
            by_date.setdefault(r["event_date"], []).append(r)
        # Sorted dates descending (newest first).
        for ev_date in sorted(by_date.keys(), reverse=True):
            group = by_date[ev_date]
            # Sort group by rank_hint desc, docno asc.
            group.sort(key=lambda r: (-(r.get("rank_hint") or 0), r.get("docno") or ""))
            start = f.tell()
            for r in group:
                # Strip internal-only fields the frontend doesn't need.
                public = {k: v for k, v in r.items()
                          if k not in ("event_paragraph_hash", "source_rank")}
                f.write(json.dumps(public, separators=(",", ":"), ensure_ascii=False) + "\n")
            end = f.tell()
            date_index[ev_date] = [start, end - start]

    # PRD-029 day-summaries: read the snapshot journal and keep the
    # latest record per event_date. Emitted inline in the idx (small —
    # ~150 bytes per day, capped at a few months of horizon).
    day_summaries: dict[str, str] = {}
    if day_summaries_journal and day_summaries_journal.exists():
        print(f"loading day-summaries journal: {day_summaries_journal}", flush=True)
        latest_t: dict[str, str] = {}
        with open(day_summaries_journal, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ed = r.get("event_date")
                t  = r.get("t", "")
                body = r.get("summary_md")
                if not ed or not body:
                    continue
                if t >= latest_t.get(ed, ""):
                    latest_t[ed] = t
                    day_summaries[ed] = body
        print(f"  {len(day_summaries):,} day summaries archived", flush=True)

    idx_payload = {
        "version": 2,
        "event_date_index": date_index,
        "day_summaries": day_summaries,
        "total_events": len(fresh),
        "horizon_days": horizon_days,
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(idx_tmp, "w", encoding="utf-8") as f:
        json.dump(idx_payload, f, separators=(",", ":"))

    os.replace(events_tmp, events_path)
    os.replace(idx_tmp, idx_path)

    n_dates = len(date_index)
    size_kb = events_path.stat().st_size / 1024
    print(f"wrote {events_path} ({len(fresh):,} events, {size_kb:.1f} KB)", flush=True)
    print(f"wrote {idx_path} ({n_dates:,} dates indexed)", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--images-store", type=Path,
                   default=DEFAULT_VOLUME / "enwiki_top1m_images.store")
    p.add_argument("--images-map", type=Path,
                   default=DEFAULT_VOLUME / "enwiki_top1m_images.map")
    p.add_argument("--entity-class", type=Path,
                   default=DEFAULT_VOLUME / "related" / "entity_class.json")
    p.add_argument("--categories", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.categories.json",
                   help="PRD-029 step 2 category sidecar; missing is fine "
                        "(events render without grouping).")
    p.add_argument("--summaries-store", type=Path,
                   default=DEFAULT_VOLUME / "summaries.store",
                   help="PRD-029 step 3 LLM-summary FlatStore; missing is "
                        "fine (events render without summary_md).")
    p.add_argument("--summaries-map", type=Path,
                   default=DEFAULT_VOLUME / "summaries.map")
    p.add_argument("--day-summaries", type=Path,
                   default=DEFAULT_TRENDING_DIR / "day_summaries.jsonl",
                   help="PRD-029 day-roundup snapshot journal; missing is "
                        "fine (no roundup served until snapshotted).")
    p.add_argument("--out-dir", type=Path,
                   default=DEFAULT_TRENDING_DIR)
    p.add_argument("--horizon-days", type=int, default=90,
                   help="Rebuild events from the last N days; older "
                        "entries stay in the journal but skip the "
                        "timeline until year-zoom ships.")
    args = p.parse_args()

    if not args.journal.exists():
        print(f"ERROR: journal not found at {args.journal}", file=sys.stderr)
        print("       fetch_trending.py must have run at least once with the "
              "PRD-029 journaling enabled.", file=sys.stderr)
        return 1
    build(args.journal, args.images_store, args.images_map,
          args.entity_class, args.out_dir,
          categories_path=args.categories,
          summaries_store=args.summaries_store,
          summaries_map=args.summaries_map,
          day_summaries_journal=args.day_summaries,
          horizon_days=args.horizon_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
