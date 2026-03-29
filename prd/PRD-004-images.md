# PRD-004: Images

**Status:** Approved  
**Checkpoint:** Will be part of checkpoint-1

---

## Problem

Results are text-only. For many topics — people, places, animals, landmarks — a thumbnail image makes results dramatically more useful and engaging.

---

## Goal

Results that have a lead image show a thumbnail. Top result gets a larger "knowledge panel" style image. Built entirely from data in the Wikipedia XML dump — no image API calls at query time.

---

## Spec

### How Wikipedia Image URLs Work
Wikipedia image filenames are in the XML dump as e.g. `[[File:Albert_Einstein_Head.jpg]]`. The CDN URL is constructed deterministically:

```python
import hashlib
filename = "Albert_Einstein_Head.jpg"
filename_encoded = filename.replace(" ", "_")
md5 = hashlib.md5(filename_encoded.encode()).hexdigest()
url = f"https://upload.wikimedia.org/wikipedia/commons/thumb/{md5[0]}/{md5[0:2]}/{filename_encoded}/300px-{filename_encoded}"
```

This is a pure formula — no network call needed to construct the URL. The image either exists at that URL or it doesn't (we handle the "doesn't" case gracefully).

Note: some images are in `wikipedia/en/` not `commons/` — we'll try commons first, which covers ~80% of cases.

### At Index Time (wiki2trec.py changes)
- Extract the first `[[File:...]]` or `[[Image:...]]` reference from each article
- Skip: flags, icons, small decorative images (heuristic: skip filenames containing "flag", "icon", "logo", "stub", "commons-logo", "wikidata")
- Construct the CDN URL using the formula above
- Output `images.json`:
```json
{
  "Albert_Einstein": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d3/Albert_Einstein_Head.jpg/300px-Albert_Einstein_Head.jpg",
  "Black_hole": "https://upload.wikimedia.org/wikipedia/commons/thumb/...",
  ...
}
```

### At Query Time (server.py changes)
- Load `images.json` at startup
- Attach `image_url` field to each result if available, `null` if not

### Frontend (index.html changes)

**Result cards with images:**
- Small thumbnail (60×60px) floated right on the result card
- If image fails to load: hidden via `onerror="this.style.display='none'"`
- Only shown if `image_url` is non-null

**Knowledge panel (top result only):**
- If top result has an image: show a larger panel above/beside the results
- Panel contains: larger image (200px wide), article title as heading, snippet text
- Collapses gracefully on mobile (stacks vertically)

---

## Out of Scope
- We are not fetching or caching images ourselves — browser fetches directly from Wikimedia CDN
- We are not handling images in `wikipedia/en/` namespace (commons only for now)
- We are not resizing or processing images
- We are not showing images for every result in the knowledge panel — only the top result

---

## Done When
- [ ] `images.json` generated during conversion
- [ ] Image URLs constructed correctly using MD5 formula (verified on 5 known articles)
- [ ] Server attaches `image_url` to results
- [ ] Result cards show thumbnail when image available
- [ ] Broken images silently hidden (onerror handler)
- [ ] Top result knowledge panel renders with image
- [ ] Panel degrades gracefully when no image (no blank space, no broken layout)
- [ ] Mobile layout doesn't break
- [ ] Spot-check: "Albert Einstein", "Mount Everest", "Tiger", "Eiffel Tower" — images appear and are correct
