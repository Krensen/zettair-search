"""
Read other-search rows from clickstream (article\tcount on stdin),
filter to articles that exist in Simple English Wikipedia,
output fake queries sorted by click count.
"""
import sys, re, json, os

WIKI_DIR = os.path.join(os.path.dirname(__file__), '../zettair/wikipedia')

# Load simplewiki title set
print("Loading simplewiki titles...", file=sys.stderr)
with open(os.path.join(WIKI_DIR, 'simplewiki_titles.txt')) as f:
    simplewiki = set(l.strip() for l in f if l.strip())
print(f"  {len(simplewiki):,} titles loaded", file=sys.stderr)

SKIP_PREFIXES = (
    'List_of','Lists_of','Index_of','Outline_of','History_of',
    'Geography_of','Demographics_of','Wikipedia:','File:',
    'Template:','Category:','Help:','Portal:','Talk:','User:','Main_Page',
)
SKIP_RE = re.compile(
    r'^(\d{4}_|\d+_|\$|.*_discography$|.*_filmography$|.*_bibliography$)',
    re.IGNORECASE
)

results = {}
total = 0
matched = 0

for line in sys.stdin:
    parts = line.rstrip('\n').split('\t')
    if len(parts) != 2: continue
    article, count_str = parts
    try: count = int(count_str)
    except: continue
    total += 1

    # Must exist in our Simple English index
    if article not in simplewiki: continue
    matched += 1

    if any(article.startswith(p) for p in SKIP_PREFIXES): continue
    if SKIP_RE.match(article): continue
    if not re.search(r'[a-zA-Z]', article): continue

    query = re.sub(r'_\(.*?\)', '', article).replace('_', ' ').strip().lower()
    words = query.split()
    if not (1 <= len(words) <= 6): continue

    results[query] = results.get(query, 0) + count

print(f"\nRows: {total:,} | In simplewiki: {matched:,} | Unique queries: {len(results):,}\n")
for q, c in sorted(results.items(), key=lambda x: -x[1])[:100]:
    print(f'{c:>10,}  {q}')
