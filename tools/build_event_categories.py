#!/usr/bin/env python3
"""PRD-029: classify events into a small fixed taxonomy.

v2 (Wikipedia-categories driven). For every (docno, event_date) in
the events journal, look up the docno in
trending/wikipedia_categories.json, score each of the article's
Wikipedia categories against the bucket rules, and pick the bucket
with the most votes. Paragraph-text heuristics remain as a low-
confidence fallback for entities the categories miss.

Output sidecar (unchanged shape, consumed by build_events_index):

    trending/events.categories.json
    {
      "version": 2,
      "categories": { "Iran:2026-05-25": "world", ... },
      "counts":     { "world": 47, ... },
      "method_counts": { "wiki": 412, "paragraph": 38, "entity_class": 12, "fallback": 5 },
      "built_at": "..."
    }

Taxonomy (unchanged): politics, business, tech, sport, science,
culture, world, other.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))

CATEGORIES = ("politics", "business", "tech", "sport",
              "science", "culture", "world", "other")
# Pre-computed insertion-order priority so tie-breaks in
# vote_from_wiki_categories() do not call CATEGORIES.index() per item.
_CATEGORY_PRIORITY = {c: i for i, c in enumerate(CATEGORIES)}


# ---------------------------------------------------------------------------
# Wikipedia-category -> bucket rules
#
# A bucket "wins" a category iff ANY of its substring patterns matches.
# Order in the dict is iteration order; we accumulate votes across an
# entity's full category list and pick the highest-voted bucket. The
# weight is multiplicative — high-signal categories vote 2, generic
# ones vote 1.
# ---------------------------------------------------------------------------

BUCKET_PATTERNS: dict[str, list[tuple[str, int]]] = {
    "sport": [
        ("football", 2), ("footballer", 2), ("association football", 2),
        ("cricket", 2), ("cricketer", 2), ("rugby", 2), ("baseball", 2),
        ("basketball", 2), ("tennis", 2), ("tennis player", 2),
        ("golf", 2), ("golfer", 2), ("boxer", 2), ("boxing", 2),
        ("athletics", 2), ("athletes", 2), ("athlete", 2),
        ("ice hockey", 2), ("hockey players", 2),
        ("formula one", 2), ("nascar", 2), ("motorsport", 2),
        ("olympic", 2), ("world cup", 2), ("grand prix", 1),
        ("sports", 1), ("sport teams", 2),
        ("managers in", 2), ("coaches of", 2), ("league", 1),
        ("championship", 1), ("tournament", 1), ("athletics players", 2),
        ("wrestlers", 2), ("wrestling", 2), ("mma", 2),
        ("mixed martial", 2), ("sumo", 2),
        ("swimmers", 2), ("cyclists", 2), ("snowboarders", 2),
        ("skiers", 2), ("formula", 1),
    ],
    "politics": [
        ("politicians", 2), ("politician", 2),
        ("political party", 2), ("political parties", 2),
        ("members of parliament", 2), ("members of the senate", 2),
        ("united states senators", 2), ("united states representatives", 2),
        ("ministers of", 2), ("prime ministers", 2),
        ("presidents of", 2), ("vice presidents of", 2),
        ("governors of", 2), ("mayors of", 2),
        ("ambassadors", 2), ("diplomats", 2),
        ("political movements", 2), ("political activists", 2),
        ("political parties established", 2),
        ("cabinet members", 2), ("government officials", 2),
        ("political scandals", 1),
        ("elections", 1), ("election", 1), ("presidential elections", 2),
        ("political philosophers", 2),
    ],
    "business": [
        ("companies", 1), ("companies based", 1),
        ("companies of", 1), ("companies established", 1),
        ("multinational companies", 2),
        ("publicly traded companies", 2),
        ("banks of", 2), ("banks established", 2),
        ("financial services companies", 2),
        ("retail companies", 2), ("supermarket chains", 2),
        ("manufacturing companies", 2),
        ("airline", 2), ("airlines of", 2),
        ("automotive companies", 2),
        ("brands", 1), ("american brands", 1),
        ("ipos", 2), ("initial public offerings", 2),
        ("billionaires", 2), ("chief executives", 2),
        ("entrepreneurs", 2), ("business executives", 2),
        ("dow jones", 2), ("s&p 500", 2),
    ],
    "tech": [
        ("software companies", 2),
        ("internet companies", 2),
        ("social media", 2), ("social networking", 2),
        ("technology companies", 2),
        ("semiconductor companies", 2),
        ("computer hardware", 2), ("computer companies", 2),
        ("video game", 1), ("video game companies", 2),
        ("artificial intelligence", 2),
        ("machine learning", 2),
        ("electronics companies", 2),
        ("e-commerce", 2),
        ("smartphone", 2),
        ("operating systems", 2),
        ("programming languages", 2),
        ("websites", 1), ("web browsers", 2),
        ("software engineers", 2), ("computer scientists", 2),
        ("cryptocurrencies", 2), ("bitcoin", 2),
    ],
    "science": [
        ("scientists", 2), ("biologists", 2), ("physicists", 2),
        ("chemists", 2), ("astronomers", 2), ("mathematicians", 2),
        ("entomologists", 2), ("microbiologists", 2),
        ("zoologists", 2), ("botanists", 2), ("geologists", 2),
        ("anthropologists", 2), ("archaeologists", 2),
        ("psychologists", 2), ("economists", 2),
        ("engineers", 1), ("inventors", 2),
        ("nobel laureates in", 2),
        ("medical researchers", 2),
        ("infectious diseases", 2), ("epidemics", 2), ("pandemics", 2),
        ("vaccines", 2), ("viruses", 2), ("virology", 2),
        ("public health", 2), ("medical organizations", 1),
        ("astronomy", 1), ("astronomical objects", 2),
        ("spaceflight", 2), ("planets", 2), ("exoplanets", 2),
        ("biology", 1), ("chemistry", 1), ("physics", 1),
        ("mathematics", 1),
        ("monoterpenes", 2), ("hydrocarbon", 2),   # chemistry stubs
        ("molecules", 2), ("compounds", 1),
        ("ecology", 1), ("paleontology", 2), ("fossils", 2),
        ("species", 2),
    ],
    "culture": [
        ("films", 2), ("film directors", 2), ("film actors", 2),
        ("actors", 2), ("actresses", 2),
        ("singers", 2), ("songwriters", 2), ("musicians", 2),
        ("rappers", 2), ("rock musicians", 2),
        ("pop musicians", 2), ("country musicians", 2),
        ("albums", 1), ("songs", 1), ("singles", 1),
        ("television series", 2), ("tv series", 2),
        ("television actors", 2), ("television presenters", 2),
        ("comedians", 2), ("authors", 2), ("novelists", 2),
        ("poets", 2), ("books", 1), ("novels", 1),
        ("video games", 1),   # tech-tagged above; culture wins on ties
        ("art exhibitions", 2), ("artists", 2),
        ("musical groups", 2), ("rock bands", 2),
        ("hip hop", 2),
        ("opera", 2), ("ballet", 2),
        ("anime", 2), ("manga", 2),
        ("internet memes", 2),
    ],
    "world": [
        ("countries", 1), ("states of", 1), ("cities in", 1),
        ("capitals", 1),
        ("wars", 2), ("battles", 2), ("conflicts", 2),
        ("massacres", 2), ("genocides", 2),
        ("terrorist attacks", 2), ("terror attacks", 2),
        ("natural disasters", 2), ("earthquakes", 2),
        ("hurricanes", 2), ("cyclones", 2),
        ("airstrikes", 2), ("bombings", 2),
        ("uprisings", 2), ("revolutions", 2),
        ("disasters in", 2), ("protests", 2),
        ("refugee", 2), ("crises in", 2),
        ("federal holidays", 1), ("national holidays", 1),
        ("public holidays", 1), ("observances", 1),
        ("murders", 2), ("homicides", 2), ("crimes", 1),
        ("kidnappings", 2),
    ],
}

# Stop-categories that should NOT vote, even when they substring-match
# a bucket pattern. These are too generic and would skew classification.
STOP_CATEGORY_PATTERNS = [
    "living people",
    "deaths",
    "births",
    "burials at",
    "establishments in",   # noisy (every company / country / building)
    "in popular culture",
]


def vote_from_wiki_categories(cats: list[str]) -> tuple[str | None, int]:
    """Score the entity's Wikipedia categories against bucket patterns.
    Returns (best_bucket_or_None, total_votes_for_best). Ties broken
    by the bucket order in BUCKET_PATTERNS (insertion order)."""
    votes: Counter = Counter()
    for raw in cats:
        c = raw.lower()
        if any(s in c for s in STOP_CATEGORY_PATTERNS):
            continue
        for bucket, patterns in BUCKET_PATTERNS.items():
            for pat, weight in patterns:
                if pat in c:
                    votes[bucket] += weight
                    break   # one vote per category per bucket
    if not votes:
        return None, 0
    # Stable tie-break by bucket order (insertion order in CATEGORIES).
    best = max(votes.items(),
               key=lambda kv: (kv[1], -_CATEGORY_PRIORITY[kv[0]]))
    return best[0], best[1]


# ---------------------------------------------------------------------------
# Paragraph-text fallback (low confidence, used when wiki cats miss)
# ---------------------------------------------------------------------------

_rx = lambda p: re.compile(p, re.IGNORECASE)
PARA_RULES = [
    ("sport",    _rx(r"\b(match|league|championship|tournament|goal|olympic|world\s+cup|grand\s+slam|grand\s+prix|coach|manager|striker|defender|midfielder|defeated|qualifier)\b")),
    ("business", _rx(r"\b(earnings|quarterly|shares?|acqui[sz]ition|merger|ipo|stock\s+price|ceo|board\s+of\s+directors|layoffs?|profits?|revenue)\b")),
    ("tech",     _rx(r"\b(ai|artificial\s+intelligence|chatbot|machine\s+learning|gpt|semiconductor|chips?|cloud\s+computing|software|smartphone|open\s+source|cybersecurity|bitcoin|crypto|blockchain|ethereum)\b")),
    ("politics", _rx(r"\b(elected|election|inaugurated|minister|prime\s+minister|senator|congress|parliament|impeach|cabinet|coalition|sworn\s+in)\b")),
    ("science",  _rx(r"\b(study|research|scientists|vaccine|virus|disease|outbreak|epidemic|pandemic|clinical\s+trial|NASA|telescope|exoplanet)\b")),
    ("culture",  _rx(r"\b(album|song|single|film|movie|director|premiered?|tour|concert|festival|novel|memoir|actor|actress|Grammy|Oscar|Emmy|BAFTA)\b")),
    ("world",    _rx(r"\b(killed|wounded|war|airstrike|missile|ceasefire|refugees?|earthquake|hurricane|cyclone|coup|protests?|riots?|murder)\b")),
]


def classify_paragraph(paragraph: str | None) -> str | None:
    if not paragraph:
        return None
    for bucket, rx in PARA_RULES:
        if rx.search(paragraph):
            return bucket
    return None


# ---------------------------------------------------------------------------
# Entity-class backstop
# ---------------------------------------------------------------------------

CLASS_BACKSTOPS = {
    "place":        "world",
    "event":        "world",
    "organisation": "business",
    "work":         "culture",
    # "human" deliberately omitted — too many cases (athletes, artists,
    # politicians, scientists). Better to fall through to "other" than
    # to mislabel.
}


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify(docno: str,
             paragraph: str | None,
             wiki_cats: list[str] | None,
             entity_class: str | None) -> tuple[str, str]:
    """Returns (bucket, method) where method is 'wiki', 'paragraph',
    'entity_class', or 'fallback'."""
    if wiki_cats:
        b, votes = vote_from_wiki_categories(wiki_cats)
        # Only trust the wiki-vote if at least one category voted.
        # votes >= 1 means we found something; below that the entity
        # only matched stop-categories.
        if b is not None and votes >= 1:
            return b, "wiki"

    pb = classify_paragraph(paragraph)
    if pb:
        return pb, "paragraph"

    if entity_class and entity_class in CLASS_BACKSTOPS:
        return CLASS_BACKSTOPS[entity_class], "entity_class"

    return "other", "fallback"


# ---------------------------------------------------------------------------
# Build driver
# ---------------------------------------------------------------------------

def build(journal_path: Path, out_path: Path,
          wiki_cache_path: Path | None = None,
          entity_class_path: Path | None = None) -> dict:
    if not journal_path.exists():
        print(f"ERROR: journal not found at {journal_path}", file=sys.stderr)
        return {}

    wiki_cats: dict[str, list[str]] = {}
    if wiki_cache_path and wiki_cache_path.exists():
        print(f"reading wikipedia categories: {wiki_cache_path}", flush=True)
        try:
            with open(wiki_cache_path, encoding="utf-8") as f:
                cache = json.load(f)
            wiki_cats = cache.get("categories", {})
            print(f"  {len(wiki_cats):,} entities with Wikipedia categories",
                  flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN: could not load wiki-cats: {e}", flush=True)

    entity_classes: dict[str, str] = {}
    if entity_class_path and entity_class_path.exists():
        print(f"reading entity classes: {entity_class_path}", flush=True)
        try:
            with open(entity_class_path, encoding="utf-8") as f:
                entity_classes = json.load(f)
            print(f"  {len(entity_classes):,} classifications loaded",
                  flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN: could not load entity-class file: {e}", flush=True)

    print(f"reading journal: {journal_path}", flush=True)
    seen: dict[str, str] = {}
    method_counts: Counter = Counter()
    n_lines = 0
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            docno = rec.get("docno")
            ev_date = rec.get("event_date")
            if not docno or not ev_date:
                continue
            key = f"{docno}:{ev_date}"
            cat, method = classify(
                docno,
                rec.get("event_paragraph"),
                wiki_cats.get(docno),
                entity_classes.get(docno),
            )
            seen[key] = cat
            method_counts[method] += 1

    counts: Counter = Counter()
    for cat in seen.values():
        counts[cat] += 1
    print(f"  scanned {n_lines:,} lines, classified {len(seen):,} unique "
          f"(docno, event_date) pairs", flush=True)
    print(f"  method breakdown: {dict(method_counts)}", flush=True)
    for cat in CATEGORIES:
        print(f"    {cat:10s} {counts[cat]:>6}", flush=True)

    payload = {
        "version": 2,
        "categories": seen,
        "counts": dict(counts),
        "method_counts": dict(method_counts),
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, out_path)
    print(f"wrote {out_path}", flush=True)
    return payload


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.jsonl")
    p.add_argument("--output", type=Path,
                   default=DEFAULT_TRENDING_DIR / "events.categories.json")
    p.add_argument("--wiki-cats", type=Path,
                   default=DEFAULT_TRENDING_DIR / "wikipedia_categories.json",
                   help="Wikipedia-categories cache built by "
                        "fetch_wikipedia_categories.py")
    p.add_argument("--entity-class", type=Path,
                   default=Path("/mnt/wikipedia-source/related/entity_class.json"),
                   help="PRD-025 entity-class JSON; backstop only")
    args = p.parse_args()
    build(args.journal, args.output, args.wiki_cats, args.entity_class)
    return 0


if __name__ == "__main__":
    sys.exit(main())
