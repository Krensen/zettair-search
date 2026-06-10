#!/usr/bin/env python3
"""Synthetic test of the freshness-aware source swap in
fetch_trending.apply_specificity_gate and the producer's stale-event
skip in build_news_summary_jobs. Monkeypatches the network fetchers;
no prod paths touched.

Run: python3 tests/test_news_freshness.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["ZET_TRENDING_DIR"] = tmp
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import fetch_trending as ft

today = dt.datetime.now(dt.UTC).date()


def days_ago(n: int) -> dt.date:
    return today - dt.timedelta(days=n)


PARA_TMPL = (
    "On {date}, the subject announced a major policy initiative that drew "
    "significant national attention and extensive coverage in the press, "
    "prompting responses from several senior officials."
)


def wiki_para(age_days: int) -> str:
    return PARA_TMPL.format(date=days_ago(age_days).strftime("%-d %B %Y"))


def headlines(age_days: int, n: int = 3) -> list[dict]:
    pub = dt.datetime.combine(days_ago(age_days), dt.time(12, 0), tzinfo=dt.UTC)
    return [
        {"title": f"Headline {i}", "source": "Reuters",
         "pub_date": pub.isoformat(), "link": "https://example.com"}
        for i in range(n)
    ]


# docno -> (wikitext, headlines, expected event_source)
CASES = {
    "Fresh_Wiki":      (wiki_para(0), headlines(0),      "wikipedia"),
    "Stale_Wiki_Hot":  (wiki_para(5), headlines(1),      "news_rss"),   # stale wiki + fresh news -> swap
    "Stale_Wiki_Cold": (wiki_para(5), headlines(5),      "wikipedia"),  # both old -> keep wiki prose
    "Stale_Wiki_Thin": (wiki_para(5), headlines(1, n=1), "wikipedia"),  # < NEWS_MIN_HEADLINES -> no swap
    "No_Wiki_Para":    ("Nothing dated here.", headlines(1), "news_rss"),  # PRD-022 path
}

def _headlines_for(query: str, today=None) -> list[dict]:
    key = next(k for k in CASES if k.lower().replace("_", " ") == query)
    return CASES[key][1]


ft.fetch_article_wikitext = lambda docno: CASES[docno][0]
ft.fetch_news_headlines_cached = _headlines_for

items = [
    {"docno": k, "title": k.replace("_", " "), "query": k.lower().replace("_", " ")}
    for k in CASES
]
kept, stats = ft.apply_specificity_gate(items)

failures = 0
for it in kept:
    got = it.get("event_source")
    want = CASES[it["docno"]][2]
    status = "OK  " if got == want else "FAIL"
    if got != want:
        failures += 1
    print(f"{status} {it['docno']:<16} source={got} event_date={it.get('event_date')}")

assert stats["swapped_stale_wiki"] == 1, stats
hot = next(i for i in kept if i["docno"] == "Stale_Wiki_Hot")
assert hot["event_date"] == days_ago(1).isoformat(), \
    f"swapped item must carry the headline date, got {hot['event_date']}"
print("gate stats:", stats)

# --- producer stale-event skip ----------------------------------------
os.environ["ZET_SUMMARIES_DIR"] = tmp + "/summaries"
os.environ["ZET_SUMMARIES_MAP"] = tmp + "/summaries.map"   # missing -> every job is "missing"
import build_news_summary_jobs as bj

current = {
    "mode": "spike",
    "items": [
        {"query": "fresh event", "title": "Fresh", "event_paragraph": "x",
         "event_date": days_ago(2).isoformat()},
        {"query": "stale event", "title": "Stale", "event_paragraph": "x",
         "event_date": days_ago(10).isoformat()},
        {"query": "no date", "title": "NoDate", "event_paragraph": "x"},
    ],
}
cur_path = tmp + "/current.json"
with open(cur_path, "w") as f:
    json.dump(current, f)

sys.argv = ["build_news_summary_jobs.py", "--dry-run", "--current", cur_path]
bj.main()

pri = Path(tmp) / "summaries" / "priority"
assert not pri.exists() or not list(pri.glob("*.json")), "--dry-run must not write jobs"

if failures:
    print(f"{failures} FAILURES")
    sys.exit(1)
print("ALL OK (producer log above should show enqueued=2 skipped_stale_event=1)")
