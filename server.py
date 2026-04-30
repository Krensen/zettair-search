"""
Zettair Search Web Service
FastAPI wrapper around the zet CLI with a persistent worker pool.

PRD-007: keeps N zet processes alive across queries — index loaded once,
queries piped via stdin, JSON Lines responses read from stdout.
PRD-011: query-biased summaries come directly from Zettair's C summariser
         via the summary field in JSON output (--summary=plain).
"""
import asyncio
import bisect
import json
import os
import re
import time
import datetime
import urllib.request
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import summarise

# --- Config (env vars with sensible defaults) ---
ZET_BINARY = os.environ.get(
    "ZET_BINARY",
    os.path.join(os.path.dirname(__file__), "../zettair/devel/zet"),
)
ZET_INDEX = os.environ.get(
    "ZET_INDEX",
    os.path.join(os.path.dirname(__file__), "../zettair/wikiindex/index"),
)
ZET_PORT         = int(os.environ.get("ZET_PORT", "8765"))
ZET_CLICK_PRIOR  = os.environ.get("ZET_CLICK_PRIOR",
    os.path.join(os.path.dirname(__file__), "../zettair/wikipedia/click_prior.bin"))
ZET_CLICK_ALPHA  = os.environ.get("ZET_CLICK_ALPHA", "0.5")
ZET_WORKERS      = int(os.environ.get("ZET_WORKERS", "2"))
ZET_QUERY_TIMEOUT = float(os.environ.get("ZET_QUERY_TIMEOUT", "5.0"))

_wiki_dir = os.path.join(os.path.dirname(__file__), "../zettair/wikipedia")

# Query + click log
_log_dir = os.path.join(os.path.dirname(__file__), "logs")
QUERY_LOG = os.environ.get("ZET_QUERY_LOG", os.path.join(_log_dir, "queries.jsonl"))
CLICK_LOG  = os.environ.get("ZET_CLICK_LOG",  os.path.join(_log_dir, "clicks.jsonl"))
CRASH_LOG  = os.environ.get("ZET_CRASH_LOG",  os.path.join(_log_dir, "zet_crashes.jsonl"))
_log_lock = asyncio.Lock()

def _ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

async def _append_log(path: str, record: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with _log_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

# Sidecar data paths
SNIPPETS_STORE_PATH = os.environ.get("ZET_SNIPPETS_STORE", os.path.join(_wiki_dir, "enwiki_snippets.store"))
SNIPPETS_MAP_PATH   = os.environ.get("ZET_SNIPPETS_MAP",   os.path.join(_wiki_dir, "enwiki_snippets.map"))
IMAGES_STORE_PATH   = os.environ.get("ZET_IMAGES_STORE",   os.path.join(_wiki_dir, "enwiki_images.store"))
IMAGES_MAP_PATH     = os.environ.get("ZET_IMAGES_MAP",     os.path.join(_wiki_dir, "enwiki_images.map"))
URLS_STORE_PATH     = os.environ.get("ZET_URLS_STORE",     os.path.join(_wiki_dir, "enwiki_urls.store"))
URLS_MAP_PATH       = os.environ.get("ZET_URLS_MAP",       os.path.join(_wiki_dir, "enwiki_urls.map"))
DOCSTORE_PATH       = os.environ.get("ZET_DOCSTORE",       os.path.join(_wiki_dir, "enwiki.docstore"))
DOCMAP_PATH         = os.environ.get("ZET_DOCMAP",         os.path.join(_wiki_dir, "enwiki.docmap"))
AUTOSUGGEST_PATH    = os.environ.get("ZET_AUTOSUGGEST",    os.path.join(_wiki_dir, "autosuggest.json"))

_autosuggest: list = []   # sorted list of (query, count) tuples

# Cache index.html at startup
_index_html: str = ""


# ---------------------------------------------------------------------------
# Flat store — random-access by docno via an offset map (snippets, images)
# ---------------------------------------------------------------------------

class FlatStore:
    """Disk-based key→value store: flat UTF-8 file + JSON offset map."""

    def __init__(self, store_path: str, map_path: str, label: str):
        self._store_path = store_path
        self._map_path = map_path
        self._label = label
        self._map: dict = {}
        self._fd: int = -1
        self._loaded = False

    def load(self):
        if not os.path.exists(self._map_path) or not os.path.exists(self._store_path):
            print(f"WARNING: {self._label} not found — will be unavailable", flush=True)
            return
        with open(self._map_path, encoding="utf-8") as f:
            self._map = json.load(f)
        self._fd = os.open(self._store_path, os.O_RDONLY)
        self._loaded = True
        size_mb = os.path.getsize(self._store_path) / 1024 / 1024
        print(f"  {self._label}: {len(self._map):,} entries, {size_mb:.0f}MB store", flush=True)

    def close(self):
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def get(self, docno: str) -> str | None:
        if not self._loaded:
            return None
        entry = self._map.get(docno)
        if entry is None:
            return None
        offset, length = entry
        try:
            # os.pread is atomic — no seek, safe under concurrent calls
            return os.pread(self._fd, length, offset).decode("utf-8", errors="replace")
        except OSError:
            return None

    def get_many(self, docnos: list[str]) -> dict[str, str]:
        return {d: t for d in docnos if (t := self.get(d)) is not None}


_snippets_store = FlatStore(SNIPPETS_STORE_PATH, SNIPPETS_MAP_PATH, "snippets")
_images_store   = FlatStore(IMAGES_STORE_PATH,   IMAGES_MAP_PATH,   "images")
_urls_store     = FlatStore(URLS_STORE_PATH,     URLS_MAP_PATH,     "urls")
_docstore       = FlatStore(DOCSTORE_PATH,       DOCMAP_PATH,       "docstore")


# ---------------------------------------------------------------------------
# Persistent Zettair worker pool (PRD-007)
# ---------------------------------------------------------------------------

class ZetWorker:
    """A single long-lived zet process."""

    def __init__(self, proc: asyncio.subprocess.Process, worker_id: int):
        self.proc = proc
        self.worker_id = worker_id
        self.busy = False
        self.queries_served = 0
        self.crashes = 0

    def is_alive(self) -> bool:
        return self.proc.returncode is None

    async def query(self, q: str, n: int) -> list[dict]:
        """Send a query, read JSON Lines until sentinel. Returns list of result dicts."""
        line = (q.strip().replace("\n", " ") + "\n").encode()
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

        results = []
        async with asyncio.timeout(ZET_QUERY_TIMEOUT):
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    raise RuntimeError(f"Worker {self.worker_id} stdout closed unexpectedly")
                obj = json.loads(raw.decode("utf-8", errors="replace"))
                if obj.get("done"):
                    results.append({"_meta": True,
                                    "total": obj.get("total", 0),
                                    "took_ms": obj.get("took_ms", 0)})
                    break
                results.append(obj)
        self.queries_served += 1
        return results


class ZetPool:
    """A fixed pool of persistent zet worker processes."""

    def __init__(self):
        self._workers: list[ZetWorker] = []
        self._sem: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()
        self._env: dict = {}
        self._args: list[str] = []

    async def start(self, size: int):
        self._sem = asyncio.Semaphore(size)
        self._env = os.environ.copy()
        if os.path.exists(ZET_CLICK_PRIOR):
            self._env["ZET_CLICK_PRIOR"] = ZET_CLICK_PRIOR
            self._env["ZET_CLICK_ALPHA"] = ZET_CLICK_ALPHA

        self._args = [
            ZET_BINARY,
            "-f", ZET_INDEX,
            "--okapi",
            "--summary=plain",
            "--output=json",
            "-n", "100",   # max results per query; Python slices to requested n
        ]

        for i in range(size):
            w = await self._spawn(i)
            self._workers.append(w)

        print(f"[zet_pool] started {size} workers", flush=True)

    async def _spawn(self, worker_id: int) -> ZetWorker:
        proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        return ZetWorker(proc, worker_id)

    async def _get_worker(self) -> ZetWorker:
        """Return a live, non-busy worker (respawning if needed)."""
        async with self._lock:
            for w in self._workers:
                if not w.busy:
                    if not w.is_alive():
                        await self._respawn_worker(w)
                    w.busy = True
                    return w
        raise RuntimeError("No available worker found")

    async def _respawn_worker(self, w: ZetWorker):
        w.crashes += 1
        print(f"[zet_pool] respawning worker {w.worker_id} (crash #{w.crashes})", flush=True)
        asyncio.create_task(_append_log(CRASH_LOG, {
            "ts": _ts(),
            "worker_id": w.worker_id,
            "crash_count": w.crashes,
        }))
        try:
            w.proc.kill()
        except Exception:
            pass
        new_proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        w.proc = new_proc

    async def run_query(self, q: str, n: int) -> dict:
        """Acquire a worker, run the query, release, return parsed result."""
        await self._sem.acquire()
        worker = await self._get_worker()
        try:
            t0 = time.monotonic()
            raw = await worker.query(q, n)
            elapsed = (time.monotonic() - t0) * 1000

            meta = next((r for r in raw if r.get("_meta")), {})
            results = [r for r in raw if not r.get("_meta")][:n]

            return {
                "total": meta.get("total", len(results)),
                "took_ms": round(meta.get("took_ms", elapsed), 2),
                "results": results,
            }
        except asyncio.TimeoutError:
            # Worker stdin/stdout are now out of sync — must respawn before reuse
            async with self._lock:
                await self._respawn_worker(worker)
            raise
        except Exception as e:
            if not worker.is_alive():
                async with self._lock:
                    await self._respawn_worker(worker)
            raise
        finally:
            worker.busy = False
            self._sem.release()

    async def shutdown(self):
        for w in self._workers:
            try:
                w.proc.stdin.close()
                w.proc.terminate()
            except Exception:
                pass
        # Wait up to 3s for graceful exit, then force-kill
        await asyncio.gather(
            *[asyncio.wait_for(w.proc.wait(), timeout=3.0) for w in self._workers],
            return_exceptions=True,
        )
        for w in self._workers:
            try:
                w.proc.kill()
            except Exception:
                pass
        print("[zet_pool] shutdown complete", flush=True)


# Global pool instance
_pool = ZetPool()


# ---------------------------------------------------------------------------
# FastAPI app with lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _index_html
    # Startup
    _snippets_store.load()
    _images_store.load()
    _urls_store.load()
    _docstore.load()
    await _load_autosuggest()
    await _pool.start(ZET_WORKERS)
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, encoding="utf-8") as f:
        _index_html = f.read()
    yield
    # Shutdown
    _snippets_store.close()
    _images_store.close()
    _urls_store.close()
    _docstore.close()
    await _pool.shutdown()


app = FastAPI(title="Zettair Search Service", lifespan=lifespan)


async def _load_autosuggest():
    global _autosuggest
    if os.path.exists(AUTOSUGGEST_PATH):
        print(f"Loading autosuggest from {AUTOSUGGEST_PATH}...", flush=True)
        with open(AUTOSUGGEST_PATH, encoding="utf-8") as f:
            _autosuggest = [tuple(x) for x in json.load(f)]
        print(f"  Loaded {len(_autosuggest):,} suggestions", flush=True)
    else:
        print(f"WARNING: autosuggest file not found: {AUTOSUGGEST_PATH}")


def enrich_results(results: list, query: str) -> list:
    """Attach url, snippet, and image. All sidecar stores are keyed by docno (safe_id).

    Snippet is generated by the inline Python summariser against the docstore.
    Falls back to the pre-baked snippets store if the docstore lookup misses
    or the summariser returns empty.
    """
    query_terms = summarise.parse_query(query)
    enriched = []
    for r in results:
        docno = r.get("docno", "")
        snippet = ""
        text = _docstore.get(docno)
        if text:
            snippet = summarise.summarise_doc(text, query_terms)
        if not snippet:
            snippet = _snippets_store.get(docno) or ""
        # URL store only contains entries for docnos where the dbkey differs
        # from the safe_id (~23% of articles). For the rest, construct it
        # from the docno directly.
        url = _urls_store.get(docno) or f"https://en.wikipedia.org/wiki/{docno}"
        enriched.append({
            "rank": r["rank"],
            "score": r["score"],
            "docid": r["docid"],
            "docno": docno,
            "url": url,
            "snippet": snippet,
            "image_url": _images_store.get(docno),
        })
    return enriched


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/suggest")
async def suggest(
    q: str = Query(..., description="Query prefix"),
    n: int = Query(8, ge=1, le=200),
):
    """Return autosuggest results for a query prefix."""
    prefix = q.strip().lower()
    if len(prefix) < 2 or not _autosuggest:
        return {"q": q, "suggestions": []}

    keys = [x[0] for x in _autosuggest]
    lo = bisect.bisect_left(keys, prefix)

    candidates = []
    i = lo
    while i < len(_autosuggest) and _autosuggest[i][0].startswith(prefix):
        candidates.append(_autosuggest[i])
        i += 1

    candidates.sort(key=lambda x: -x[1])
    suggestions = [{"query": qstr, "count": c} for qstr, c in candidates[:n]]
    return {"q": q, "suggestions": suggestions}


@app.get("/search")
async def search(
    q: str = Query(..., description="Search query"),
    n: int = Query(10, ge=1, le=100, description="Number of results"),
):
    if not q.strip():
        return JSONResponse({"error": "Empty query"}, status_code=400)

    try:
        parsed = await _pool.run_query(q.strip(), n)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Query timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    results = enrich_results(parsed["results"], q.strip())

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
        # Run blocking urllib call in a thread so the event loop isn't blocked
        req = urllib.request.Request(url, headers={
            "User-Agent": "ZettairSearch/1.0 (https://zettair.io)",
            "Referer": "https://zettair.io/",
        })
        loop = asyncio.get_event_loop()
        def _fetch():
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read(), r.headers.get("Content-Type", "image/jpeg")
        data, content_type = await loop.run_in_executor(None, _fetch)
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    return _index_html


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ZET_PORT)
