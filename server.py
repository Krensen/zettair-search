"""
Zettair Search Web Service
FastAPI wrapper around the zet CLI with async queuing.
"""
import asyncio
import json
import os
import re
import datetime
import urllib.request
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

# --- Config (env vars with sensible defaults) ---
ZET_BINARY = os.environ.get(
    "ZET_BINARY",
    os.path.join(os.path.dirname(__file__), "../zettair/devel/zet"),
)
ZET_INDEX = os.environ.get(
    "ZET_INDEX",
    os.path.join(os.path.dirname(__file__), "../zettair/wikiindex/index"),
)
ZET_PORT = int(os.environ.get("ZET_PORT", "8765"))

# Query + click log
_log_dir = os.path.join(os.path.dirname(__file__), "logs")
QUERY_LOG = os.environ.get("ZET_QUERY_LOG", os.path.join(_log_dir, "queries.jsonl"))
CLICK_LOG  = os.environ.get("ZET_CLICK_LOG",  os.path.join(_log_dir, "clicks.jsonl"))
_log_lock = asyncio.Lock()

def _ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

async def _append_log(path: str, record: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with _log_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

# Sidecar data paths (alongside the XML source)
_wiki_dir = os.path.join(os.path.dirname(__file__), "../zettair/wikipedia")
SNIPPETS_PATH = os.environ.get("ZET_SNIPPETS", os.path.join(_wiki_dir, "simplewiki_snippets.json"))
IMAGES_PATH   = os.environ.get("ZET_IMAGES",   os.path.join(_wiki_dir, "simplewiki_images.json"))

app = FastAPI(title="Zettair Search Service")

# Serialize access to the zet subprocess
_lock = asyncio.Lock()

# Sidecar data loaded at startup
_snippets: dict = {}
_images: dict = {}


@app.on_event("startup")
async def load_sidecars():
    global _snippets, _images
    if os.path.exists(SNIPPETS_PATH):
        print(f"Loading snippets from {SNIPPETS_PATH}...", flush=True)
        with open(SNIPPETS_PATH, encoding="utf-8") as f:
            _snippets = json.load(f)
        print(f"  Loaded {len(_snippets):,} snippets", flush=True)
    else:
        print(f"WARNING: snippets file not found: {SNIPPETS_PATH}")

    if os.path.exists(IMAGES_PATH):
        print(f"Loading images from {IMAGES_PATH}...", flush=True)
        with open(IMAGES_PATH, encoding="utf-8") as f:
            _images = json.load(f)
        print(f"  Loaded {len(_images):,} images", flush=True)
    else:
        print(f"WARNING: images file not found: {IMAGES_PATH}")


def parse_zet_output(output: str) -> dict:
    """
    Parse zet stdout into structured results.
    """
    results = []
    total = 0
    took_ms = None

    header_re = re.compile(
        r"^(\d+)\.\s+(.*?)\s+\(score\s+([\d.]+),\s+docid\s+(\d+)\)"
    )
    summary_re = re.compile(r"^(\d+) results of (\d+) shown \(took ([\d.]+) seconds\)")

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw[2:] if raw.startswith("> ") else raw
        line = line.strip()

        m = header_re.match(line)
        if m:
            rank = int(m.group(1))
            title = m.group(2).strip()
            score = float(m.group(3))
            docid = int(m.group(4))
            # Zettair's own snippet (fallback)
            zet_snippet = ""
            if i + 1 < len(lines):
                zet_snippet = lines[i + 1].strip().strip('"')
                i += 1
            results.append({
                "rank": rank,
                "score": score,
                "docid": docid,
                "title": title,
                "zet_snippet": zet_snippet,
            })
            i += 1
            continue

        m = summary_re.match(line)
        if m:
            total = int(m.group(2))
            took_ms = round(float(m.group(3)) * 1000, 3)

        i += 1

    return {"total": total, "took_ms": took_ms, "results": results}


def enrich_results(results: list) -> list:
    """Attach snippets and images from sidecar data."""
    enriched = []
    for r in results:
        docno = r["title"]  # title from zet output is the DOCNO
        enriched.append({
            "rank": r["rank"],
            "score": r["score"],
            "docid": r["docid"],
            "docno": docno,
            "snippet": _snippets.get(docno) or r["zet_snippet"],
            "image_url": _images.get(docno),
        })
    return enriched


async def run_zet(query: str, n: int) -> dict:
    """Run zet as a subprocess, parse and return results."""
    async with _lock:
        proc = await asyncio.create_subprocess_exec(
            ZET_BINARY,
            "-f", ZET_INDEX,
            "--okapi",
            "--summary=plain",
            "-n", str(n),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=(query + "\n").encode())

    return parse_zet_output(stdout.decode("utf-8", errors="replace"))


@app.get("/search")
async def search(
    q: str = Query(..., description="Search query"),
    n: int = Query(10, ge=1, le=100, description="Number of results"),
):
    if not q.strip():
        return JSONResponse({"error": "Empty query"}, status_code=400)
    parsed = await run_zet(q.strip(), n)
    results = enrich_results(parsed["results"])

    # Log the query (fire-and-forget)
    asyncio.create_task(_append_log(QUERY_LOG, {
        "ts": _ts(),
        "q": q.strip(),
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
    }))

    return {
        "query": q,
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
        "results": results,
    }


class ClickEvent(BaseModel):
    q: str
    docno: str
    rank: int
    score: float


@app.post("/click")
async def click(event: ClickEvent):
    """Log a result click."""
    asyncio.create_task(_append_log(CLICK_LOG, {
        "ts": _ts(),
        "q": event.q,
        "docno": event.docno,
        "rank": event.rank,
        "score": event.score,
    }))
    return {"ok": True}


@app.get("/img")
async def image_proxy(url: str = Query(...)):
    """Proxy Wikimedia images to avoid browser-side rate limiting."""
    if not url.startswith("https://upload.wikimedia.org/"):
        return Response(status_code=403)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ZettairSearch/1.0 (https://search.hughwilliams.com)",
            "Referer": "https://search.hughwilliams.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ZET_PORT)
