"""
Query-biased summariser — Python port of ints/summarise.c
(Turpin, Hawking & Williams, SIGIR 2003)

Algorithm:
  1. Split document text into sentence fragments at punctuation boundaries
  2. Score each fragment: hits / length  (query term density)
  3. Return top SHOW_FRAGS fragments joined with " ... "

Called as a persistent subprocess by server.py via SummarisePool.
Reads one JSON line per query from stdin, writes one JSON line per response.

Input:  {"id": "...", "terms": ["black", "hole"], "docs": {"Black_hole": "full text...", ...}}
Output: {"id": "...", "summaries": {"Black_hole": "...snippet...", ...}}

Designed to run as a long-lived process — reads until stdin closes.
"""

import sys
import json
import re
import string

# Sentence-ending punctuation (mirrors ints/summarise.c: z < 12 = sentence boundary)
SENT_END = re.compile(r'[.!?]')

# Fragment score threshold — skip fragments with no query hits
MIN_HITS = 1

# How many top fragments to show
SHOW_FRAGS = 3

# Target snippet length in characters (soft limit — we don't truncate mid-sentence)
TARGET_CHARS = 300

# Stop words (common English words unlikely to be useful query terms)
STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'it', 'its', 'this',
    'that', 'these', 'those', 'as', 'not', 'also', 'about', 'into', 'than',
    'then', 'so', 'if', 'when', 'which', 'who', 'what', 'how', 'all', 'some',
    'no', 'up', 'out', 'more', 'other', 'such', 'their', 'they', 'them',
    'he', 'she', 'we', 'you', 'i', 'his', 'her', 'our', 'your', 'its',
    'there', 'here', 'where', 'while', 'during', 'after', 'before', 'between',
    'through', 'over', 'under', 'one', 'two', 'three', 'new', 'first',
}


def split_fragments(text: str) -> list[str]:
    """Split text into sentence fragments at sentence-ending punctuation."""
    # Split on sentence boundaries, keeping the punctuation
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    # Also split on newlines (paragraph breaks act like sentence endings)
    fragments = []
    for part in parts:
        sub = [s.strip() for s in part.split('\n') if s.strip()]
        fragments.extend(sub)
    return [f for f in fragments if len(f) > 10]  # skip tiny fragments


def score_fragment(fragment: str, query_terms: set[str]) -> float:
    """Score a fragment: hits / length (query term density)."""
    words = fragment.lower().split()
    if not words:
        return 0.0
    hits = sum(1 for w in words if re.sub(r'[^a-z0-9]', '', w) in query_terms)
    return hits / len(words)


def summarise_doc(text: str, query_terms: set[str]) -> str:
    """Generate a query-biased summary for one document."""
    fragments = split_fragments(text)
    if not fragments:
        return text[:TARGET_CHARS]

    # Score all fragments
    scored = [(score_fragment(f, query_terms), f) for f in fragments]

    # Keep only fragments with at least one hit
    scored = [(s, f) for s, f in scored if s > 0]

    if not scored:
        # No query terms found — return the first sentence or two
        return ' '.join(fragments[:2])[:TARGET_CHARS]

    # Pick top SHOW_FRAGS by score (stable sort preserves order for ties)
    # We want the best fragments but also want them to read naturally,
    # so after selecting the top N, re-sort by their original position.
    top_scores = sorted(scored, key=lambda x: -x[0])[:SHOW_FRAGS]
    top_texts = set(f for _, f in top_scores)

    # Re-order by original document position
    ordered = [f for f in fragments if f in top_texts]

    # Join with ellipsis separator
    snippet = ' \u2026 '.join(ordered)

    # Trim to target length (at word boundary)
    if len(snippet) > TARGET_CHARS * 2:
        snippet = snippet[:TARGET_CHARS * 2].rsplit(' ', 1)[0] + '\u2026'

    return snippet


def process_query(line: str) -> str:
    """Process one JSON query line, return one JSON response line."""
    try:
        req = json.loads(line)
        req_id = req.get('id', '')
        raw_terms = req.get('terms', [])
        docs = req.get('docs', {})

        # Normalise query terms: lowercase, strip punctuation, remove stopwords
        query_terms = set()
        for t in raw_terms:
            t = t.lower().strip(string.punctuation)
            if t and t not in STOPWORDS and len(t) > 1:
                query_terms.add(t)

        summaries = {}
        for docno, text in docs.items():
            if not text:
                summaries[docno] = ''
            else:
                summaries[docno] = summarise_doc(text, query_terms)

        return json.dumps({'id': req_id, 'summaries': summaries}, ensure_ascii=False)

    except Exception as e:
        return json.dumps({'id': '', 'error': str(e)})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        result = process_query(line)
        print(result, flush=True)


if __name__ == '__main__':
    main()
