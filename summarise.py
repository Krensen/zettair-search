"""
Query-biased summariser — Python port of ints/summarise.c
(Turpin, Hawking & Williams, SIGIR 2003)

Algorithm:
  1. Split document text into sentence fragments at punctuation boundaries
  2. Score each fragment: hits / length  (query term density)
  3. Return top SHOW_FRAGS fragments joined with " ... "

Imported by server.py and called inline from enrich_results().
Public entry points: parse_query(query_str), summarise_doc(text, query_terms).
"""

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

# Patterns that mark a fragment as reference/citation noise.
_RE_CITATION_FRAG = re.compile(
    r'\bISBN\b'
    r'|^[A-Z][a-z]+,\s+[A-Z].*?\b(19|20)\d{2}\b'   # Surname, Initial YYYY
    r'|\bpp?\.\s*\d+'                              # p. 48
    r'|\bVol\b.*?\bNo\b'                           # Vol X No Y
    r'|\bJournal\s+of\b'
    r'|\bUniversity\s+Press\b'
    r'|\bSitzungsberichte\b'
)

# Verb-like function words. A fragment without one is almost certainly
# a title, heading, name, or citation fragment.
_PROSE_VERBS = frozenset({
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'has', 'have', 'had',
    'does', 'do', 'did',
    'became', 'become', 'becomes',
    'found', 'made', 'used', 'known', 'called', 'named',
    'built', 'created', 'formed', 'caused', 'resulted',
    'worked', 'helped', 'lived', 'died', 'born', 'won', 'lost',
    'said', 'wrote', 'published', 'developed', 'discovered', 'invented',
    'showed', 'came', 'went', 'left', 'started', 'began', 'ended',
    'gave', 'took', 'put', 'set', 'led', 'brought', 'meant',
    'can', 'could', 'will', 'would', 'may', 'might',
    'includes', 'include', 'included',
    'contains', 'contain', 'contained',
})

# Fast char-class strippers via str.translate (≈10× faster than re.sub).
# Build deletion tables for "non-alphanumeric" and "non-alpha".
_KEEP_ALNUM = string.ascii_lowercase + string.digits
_KEEP_ALPHA = string.ascii_lowercase
_DEL_NONALNUM = str.maketrans('', '', ''.join(c for c in map(chr, range(256)) if c not in _KEEP_ALNUM))
_DEL_NONALPHA = str.maketrans('', '', ''.join(c for c in map(chr, range(256)) if c not in _KEEP_ALPHA))

# Sentence boundary regex, compiled once.
_RE_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z(])')


def split_fragments(text: str) -> list[str]:
    """Split text into sentence fragments. No quality filter — that runs later."""
    fragments = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for p in _RE_SENTENCE_SPLIT.split(line):
            p = p.strip()
            if p:
                fragments.append(p)
    return fragments


def _score_and_check(fragment: str, query_terms: frozenset) -> float:
    """
    Combined scorer + prose filter, single pass over words.

    Returns hits/length if the fragment is prose-quality AND has at least
    one query-term hit, else 0.

    This is the inner loop and runs over every fragment of every document.
    Optimised for the common case where most fragments have zero hits:
    walk words, count hits and verb-presence simultaneously, and bail fast
    if no hits found (no need for further filtering — we'd discard it).
    """
    if len(fragment) < 30:
        return 0.0

    lower = fragment.lower()
    words = lower.split()
    if not words:
        return 0.0

    hits = 0
    has_verb = False
    for w in words:
        # Strip non-alphanumerics for query-term match (fast path: most words
        # are pure alpha so translate is a no-op).
        clean = w.translate(_DEL_NONALNUM) if not w.isalnum() else w
        if clean in query_terms:
            hits += 1
        # Verb check: drop digits/punctuation, see if the resulting alpha-
        # only token is a verb. Same translate trick.
        if not has_verb:
            alpha = w.translate(_DEL_NONALPHA) if not w.isalpha() else w
            if alpha in _PROSE_VERBS:
                has_verb = True

    if hits == 0:
        # Score is zero either way; no need for further filtering.
        return 0.0
    if not has_verb:
        return 0.0

    # Cheaper alpha-fraction check than per-char isalpha(): ratio of
    # translated-length to original.
    alpha_chars = len(lower.translate(_DEL_NONALPHA))
    if alpha_chars / len(fragment) < 0.45:
        return 0.0

    if _RE_CITATION_FRAG.search(fragment):
        return 0.0

    return hits / len(words)


def summarise_doc(text: str, query_terms: set | frozenset) -> str:
    """Generate a query-biased summary for one document."""
    if not isinstance(query_terms, frozenset):
        query_terms = frozenset(query_terms)

    fragments = split_fragments(text)
    if not fragments:
        return text[:TARGET_CHARS]

    # Single pass: score + prose-filter every fragment.
    scored = [(s, i, f) for i, f in enumerate(fragments)
              for s in [_score_and_check(f, query_terms)]
              if s > 0.0]

    if not scored:
        # No query terms found — return the first fragment or two.
        return ' '.join(fragments[:2])[:TARGET_CHARS]

    # Take top SHOW_FRAGS by score, then re-order by original position
    # so the snippet reads naturally.
    top = sorted(scored, key=lambda x: -x[0])[:SHOW_FRAGS]
    top.sort(key=lambda x: x[1])  # by position
    snippet = ' … '.join(f for _, _, f in top)

    if len(snippet) > TARGET_CHARS * 2:
        snippet = snippet[:TARGET_CHARS * 2].rsplit(' ', 1)[0] + '…'

    return snippet


def parse_query(query: str) -> frozenset:
    """Lowercase, strip punctuation, drop stopwords and 1-char terms."""
    terms = set()
    for t in query.split():
        t = t.lower().strip(string.punctuation)
        if t and t not in STOPWORDS and len(t) > 1:
            terms.add(t)
    return frozenset(terms)
