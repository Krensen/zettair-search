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

# How many top fragments to show
SHOW_FRAGS = 3

# Target snippet length in characters (soft limit)
TARGET_CHARS = 300

# Stop words
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

# Patterns that mark a fragment as reference/citation noise — mirrors the
# build_docstore.py line-level filter but applied at fragment level in case
# any citations survived into the docstore.
_RE_CITATION_FRAG = re.compile(
    r'\bISBN\b'
    r'|^[A-Z][a-z]+,\s+[A-Z].*?\b(19|20)\d{2}\b'   # Surname, Initial YYYY
    r'|\bpp?\.\s*\d+'                                 # p. 48
    r'|\bVol\b.*?\bNo\b'                              # Vol X No Y
    r'|\bJournal\s+of\b'
    r'|\bUniversity\s+Press\b'
    r'|\bSitzungsberichte\b'
)

# Verb-like function words that almost always appear in real prose sentences.
# A fragment lacking any of these is almost certainly a title, heading, or citation.
_PROSE_VERBS = {
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'has', 'have', 'had',
    'does', 'do', 'did',
    'became', 'become', 'becomes',
    'found', 'made', 'used', 'known', 'called', 'named',
    'built', 'created', 'formed', 'caused', 'resulted',
    'worked', 'helped', 'lived', 'died', 'born', 'won', 'lost',
    'said', 'wrote', 'published', 'developed', 'discovered', 'invented',
    'showed', 'showed', 'came', 'went', 'left', 'started', 'began', 'ended',
    'gave', 'took', 'put', 'set', 'led', 'led', 'brought', 'meant',
    'can', 'could', 'will', 'would', 'may', 'might',
    'includes', 'include', 'included',
    'contains', 'contain', 'contained',
}

def _is_prose(fragment: str) -> bool:
    """
    Return True if the fragment looks like genuine prose (worth showing in a snippet).
    Filters out: citation titles, headings, bare names, short date strings.
    """
    if not fragment:
        return False

    # Must be at least 30 chars — shorter is almost certainly a heading or date
    if len(fragment) < 30:
        return False

    # Must have at least 45% alpha characters
    alpha = sum(1 for c in fragment if c.isalpha())
    if alpha / len(fragment) < 0.45:
        return False

    # Reject citation noise that survived docstore cleaning
    if _RE_CITATION_FRAG.search(fragment):
        return False

    # Must contain at least one verb-like word — the key prose signal.
    # Without a verb it's a title, heading, name, or citation fragment.
    words = set(re.sub(r'[^a-z]', '', w) for w in fragment.lower().split())
    if not words & _PROSE_VERBS:
        return False

    return True


def split_fragments(text: str) -> list[str]:
    """
    Split text into sentence fragments at sentence-ending punctuation,
    mimicking the C parser's sentence-boundary detection.

    The C ints/summarise.c splits at punctuation codes < 12, which correspond
    to sentence-ending characters (.  !  ?  and paragraph/newline boundaries).
    We replicate that here: split on [.!?] followed by whitespace + capital,
    and also on bare newlines (paragraph breaks).
    """
    # First split on newlines — each line may be a separate sentence/item
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    fragments = []
    for line in lines:
        # Split within each line on sentence boundaries:
        # period/!/? followed by space and a capital letter (or end of string)
        parts = re.split(r'(?<=[.!?])\s+(?=[A-Z(])', line)
        fragments.extend(p.strip() for p in parts if p.strip())

    # Filter to prose-quality fragments only
    return [f for f in fragments if _is_prose(f)]


def score_fragment(fragment: str, query_terms: set[str]) -> float:
    """
    Score a fragment: hits / length (query term density).
    Mirrors the C: fragments[fragcount].score = (float)hits / (float)fragpos
    where fragpos counts word+punct pairs (so roughly word count).
    """
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
