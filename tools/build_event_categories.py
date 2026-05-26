#!/usr/bin/env python3
"""PRD-029: classify events into a small fixed taxonomy.

Heuristic-only v1. Walks the trending journal, picks a category for
every (docno, event_date), and writes a flat sidecar file:

    trending/events.categories.json
    {
      "version": 1,
      "categories": {
        "Iran:2026-05-25":      "world",
        "OpenAI:2026-05-25":    "tech",
        ...
      },
      "built_at": "..."
    }

The events-index builder reads this and joins the category onto each
event record. Frontend groups within a day by category.

Taxonomy (fixed, 8 buckets):
    politics, business, tech, sport, science, culture, world, other

This is intentionally cheap. ~70% of events will land in a reasonable
bucket; the rest fall to "other". A future v2 could replace the
regex matchers with LLM classification — the file format is stable
either way.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_TRENDING_DIR = Path(os.environ.get(
    "ZET_TRENDING_DIR", "/mnt/wikipedia-source/trending",
))

CATEGORIES = ("politics", "business", "tech", "sport",
              "science", "culture", "world", "other")


# Order matters: first matching rule wins. Each rule is (category,
# entity_substring_re_or_None, paragraph_substring_re_or_None).
# Empty/None means "do not require that signal."
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# Known-entity allowlists (low false-positive rate; checked first).
TECH_ENTITIES = {
    "openai", "anthropic", "google", "alphabet", "apple", "apple_inc",
    "microsoft", "meta_platforms", "amazon", "tesla", "nvidia",
    "spacex", "starlink", "x_corp", "tiktok", "youtube", "github",
    "stripe", "shopify", "uber", "airbnb", "linkedin", "reddit",
    "instagram", "facebook", "whatsapp", "deepmind", "stability_ai",
    "anduril", "perplexity_ai", "mistral_ai", "hugging_face",
    "sam_altman", "elon_musk", "tim_cook", "mark_zuckerberg",
    "sundar_pichai", "satya_nadella", "jeff_bezos",
}
SPORT_PARAGRAPH_HINTS = _rx(
    r"\b(match|league|championship|championships|tournament|season|"
    r"goal|goals|knockout|wicket|wickets|innings|playoff|playoffs|"
    r"olympic|world\s+cup|grand\s+slam|grand\s+prix|relegation|"
    r"runs?\s+scored|points?\s+scored|coach|manager|captain|striker|"
    r"defender|midfielder|goalkeeper|quarterback|striker|fielder|"
    r"world\s+record|gold\s+medal|silver\s+medal|bronze\s+medal|"
    r"defeated|beat|trailed|qualified|qualifier)\b"
)
SPORT_ENTITY_HINTS = _rx(
    r"(_FC|_F\.C\.|_AFC|_RFC|_United$|_City$|_Rovers$|_Wanderers$|"
    r"_County$|_Town$|_Athletic$|_Albion$|_Stadium$|"
    r"_national_football_team|_cricket_team|_rugby|_basketball|"
    r"NFL|NBA|MLB|NHL|UFC|F1|Formula_One|"
    r"PGA|LPGA|ATP|WTA|UEFA|FIFA|IPL|IOC|"
    r"Open$|Championship$|Trophy$|League$)"
)
BUSINESS_PARAGRAPH_HINTS = _rx(
    r"\b(earnings|quarterly\s+results?|share\s+price|shareholders?|"
    r"acqui[sz]ition|merger|IPO|stock|bonds?|interest\s+rate|"
    r"central\s+bank|federal\s+reserve|inflation|recession|"
    r"market\s+cap|valuation|funding\s+round|series\s+[abcd]|"
    r"CEO|CFO|board\s+of\s+directors|antitrust|"
    r"layoffs?|hir(e|ing)|fired|profits?|revenue|loss|deficit|"
    r"trade\s+deal|tariff|sanctions?)\b"
)
BUSINESS_ENTITY_HINTS = _rx(
    r"(_Inc\.?$|_Corporation$|_Corp\.?$|_LLC$|_Ltd\.?$|"
    r"_Company$|_Holdings$|_Group$|_PLC$|_S\.A\.?$|_AG$)"
)
TECH_PARAGRAPH_HINTS = _rx(
    r"\b(artificial\s+intelligence|large\s+language\s+model|GPT|"
    r"chatbot|machine\s+learning|neural\s+network|"
    r"semiconductor|chips?|silicon|datacenter|cloud\s+computing|"
    r"software|operating\s+system|browser|smartphone|"
    r"open\s+source|repository|developer|engineer|product\s+launch|"
    r"data\s+breach|cybersecurity|encryption|VPN|Bitcoin|"
    r"crypto|blockchain|NFT|ethereum|stablecoin)\b"
)
POLITICS_PARAGRAPH_HINTS = _rx(
    r"\b(elected|election|elections|re-elected|inaugurated|"
    r"minister|prime\s+minister|chancellor|president|presidency|"
    r"senate|senator|congress|congressman|congresswoman|"
    r"parliament|MP|MPs|MEP|MEPs|"
    r"resigned|sworn\s+in|impeach|impeachment|vote|voted|"
    r"campaign|candidate|primary|caucus|polls?|"
    r"governor|mayor|cabinet|coalition|treaty|"
    r"diplomat|ambassador|foreign\s+minister|secretary\s+of\s+state)\b"
)
SCIENCE_PARAGRAPH_HINTS = _rx(
    r"\b(study\s+published|research(ers?|\s+team)?|scientists?|"
    r"vaccine|virus|outbreak|epidemic|pandemic|disease|"
    r"clinical\s+trial|FDA|EMA|WHO|peer-reviewed|"
    r"genome|DNA|RNA|species|fossil|paleontolog|biology|"
    r"NASA|ESA|telescope|astronomy|astronomer|asteroid|comet|"
    r"planet|exoplanet|orbit|launch\s+pad)\b"
)
CULTURE_PARAGRAPH_HINTS = _rx(
    r"\b(album|song|single|track|hit|chart|chart-topping|"
    r"film|movie|director|screenplay|box\s+office|"
    r"premiered?|released|sequel|prequel|reboot|spin-off|"
    r"book|novel|memoir|bestseller|literary|"
    r"actor|actress|cast|starred|starring|"
    r"festival|concert|tour|tickets?|theatre|theater|"
    r"art\s+exhibition|gallery|painting|sculpture|"
    r"Grammy|Oscar|Emmy|BAFTA|Cannes|Sundance|Tony)\b"
)
WORLD_PARAGRAPH_HINTS = _rx(
    r"\b(killed|deaths?|casualt(y|ies)|wounded|injured|"
    r"war|warfare|battle|skirmish|airstrike|bombing|missile|"
    r"ceasefire|truce|peace\s+deal|hostages?|prisoners?|"
    r"refugees?|migrants?|displaced|evacuation|"
    r"earthquake|tsunami|flooding?|hurricane|cyclone|typhoon|"
    r"famine|drought|wildfire|wildfires|"
    r"genocide|atrocity|coup|protests?|protested|riots?)\b"
)


def classify(docno: str, title: str, paragraph: str | None,
             entity_class: str | None = None) -> str:
    """Pick the best category for one event. First-match wins; the
    rule order encodes our priorities. entity_class (from PRD-025)
    is a low-confidence backstop when no paragraph rule fires."""
    docno_l = (docno or "").lower()
    para = paragraph or ""

    # High-confidence entity matches first.
    if docno_l in TECH_ENTITIES:
        return "tech"

    # Then paragraph + entity heuristics. Order: sport (strong hints,
    # rarely false-positives) -> tech -> business -> politics ->
    # science -> culture -> world -> other.
    if SPORT_ENTITY_HINTS.search(docno or ""):
        return "sport"
    if SPORT_PARAGRAPH_HINTS.search(para):
        return "sport"

    if TECH_PARAGRAPH_HINTS.search(para):
        return "tech"

    if BUSINESS_ENTITY_HINTS.search(docno or ""):
        return "business"
    if BUSINESS_PARAGRAPH_HINTS.search(para):
        return "business"

    if POLITICS_PARAGRAPH_HINTS.search(para):
        return "politics"

    if SCIENCE_PARAGRAPH_HINTS.search(para):
        return "science"

    if CULTURE_PARAGRAPH_HINTS.search(para):
        return "culture"

    if WORLD_PARAGRAPH_HINTS.search(para):
        return "world"

    # Backstops from PRD-025 entity class. Lower confidence — only
    # used when no paragraph rule has fired. A "place" event with no
    # other signal is almost always foreign-affairs / world news.
    if entity_class == "place":
        return "world"
    if entity_class == "organisation":
        return "business"
    if entity_class == "work":
        return "culture"
    if entity_class == "event":
        return "world"
    # "human" with no other signal -> politics is too aggressive;
    # leave as other so we don't mislabel athletes / artists.

    return "other"


def build(journal_path: Path, out_path: Path,
          entity_class_path: Path | None = None) -> dict:
    if not journal_path.exists():
        print(f"ERROR: journal not found at {journal_path}", file=sys.stderr)
        return {}
    entity_classes: dict[str, str] = {}
    if entity_class_path and entity_class_path.exists():
        print(f"reading entity classes: {entity_class_path}", flush=True)
        try:
            with open(entity_class_path, encoding="utf-8") as f:
                entity_classes = json.load(f)
            print(f"  {len(entity_classes):,} classifications loaded", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN: couldn't load entity-class file: {e}", flush=True)

    print(f"reading journal: {journal_path}", flush=True)
    seen: dict[str, str] = {}   # key=docno:event_date -> category
    counts: Counter = Counter()
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
            cat = classify(docno, rec.get("title"),
                           rec.get("event_paragraph"),
                           entity_classes.get(docno))
            seen[key] = cat   # last write wins; latest paragraph drives the choice
    for cat in seen.values():
        counts[cat] += 1
    print(f"  scanned {n_lines:,} lines, classified {len(seen):,} unique "
          f"(docno, event_date) pairs", flush=True)
    for cat in CATEGORIES:
        print(f"    {cat:10s} {counts[cat]:>6}", flush=True)

    payload = {
        "version": 1,
        "categories": seen,
        "counts": dict(counts),
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
    p.add_argument("--entity-class", type=Path,
                   default=Path("/mnt/wikipedia-source/related/entity_class.json"),
                   help="PRD-025 entity-class JSON; missing is fine (skip backstop)")
    args = p.parse_args()
    build(args.journal, args.output, args.entity_class)
    return 0


if __name__ == "__main__":
    sys.exit(main())
