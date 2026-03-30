"""
Zettair Search Web Service
FastAPI wrapper around the zet CLI with a persistent worker pool.

PRD-007: keeps N zet processes alive across queries — index loaded once,
queries piped via stdin, JSON Lines responses read from stdout.
PRD-008: optional query-biased summariser (ZET_SUMMARISE=1) using summarise.py
"""
import asyncio
import bisect
import json
import os
import re
import time
import datetime
import string
import urllib.request
from contextlib import asynccontextmanager
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
ZET_PORT         = int(os.environ.get("ZET_PORT", "8765"))
ZET_CLICK_PRIOR  = os.environ.get("ZET_CLICK_PRIOR",
    os.path.join(os.path.dirname(__file__), "../zettair/wikipedia/click_prior.bin"))
ZET_CLICK_ALPHA  = os.environ.get("ZET_CLICK_ALPHA", "0.5")
ZET_WORKERS      = int(os.environ.get("ZET_WORKERS", "2"))
ZET_QUERY_TIMEOUT = float(os.environ.get("ZET_QUERY_TIMEOUT", "5.0"))

# PRD-008: query-biased summariser
ZET_SUMMARISE  = os.environ.get("ZET_SUMMARISE", "0") == "1"
SUMMARISE_PY   = os.path.join(os.path.dirname(__file__), "summarise.py")
_wiki_dir      = os.path.join(os.path.dirname(__file__), "../zettair/wikipedia")
DOCSTORE_PATH  = os.environ.get("ZET_DOCSTORE",  os.path.join(_wiki_dir, "simplewiki.docstore"))
DOCMAP_PATH    = os.environ.get("ZET_DOCMAP",    os.path.join(_wiki_dir, "simplewiki.docmap"))
SUMM_TIMEOUT   = float(os.environ.get("ZET_SUMM_TIMEOUT", "2.0"))

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
SNIPPETS_PATH    = os.environ.get("ZET_SNIPPETS",    os.path.join(_wiki_dir, "simplewiki_snippets.json"))
IMAGES_PATH      = os.environ.get("ZET_IMAGES",      os.path.join(_wiki_dir, "simplewiki_images.json"))
AUTOSUGGEST_PATH = os.environ.get("ZET_AUTOSUGGEST", os.path.join(_wiki_dir, "autosuggest.json"))

# Sidecar data loaded at startup
_snippets: dict = {}
_images: dict = {}
_autosuggest: list = []   # sorted list of (query, count) tuples


# ---------------------------------------------------------------------------
# Docstore reader — random-access full article text (PRD-008)
# ---------------------------------------------------------------------------

class DocStore:
    """Memory-maps the docstore file; random-access by docno via docmap."""

    def __init__(self):
        self._docmap: dict = {}
        self._fp = None
        self._loaded = False

    def load(self):
        if not os.path.exists(DOCMAP_PATH) or not os.path.exists(DOCSTORE_PATH):
            print(f"WARNING: docstore not found — summariser will use pre-baked snippets")
            return
        with open(DOCMAP_PATH, encoding="utf-8") as f:
            self._docmap = json.load(f)
        self._fp = open(DOCSTORE_PATH, "rb")
        self._loaded = True
        print(f"  Docstore loaded: {len(self._docmap):,} docs, {os.path.getsize(DOCSTORE_PATH)/1024/1024:.0f}MB", flush=True)

    def get(self, docno: str) -> str | None:
        """Return full text for a docno, or None if not found."""
        if not self._loaded:
            return None
        entry = self._docmap.get(docno)
        if entry is None:
            return None
        offset, length = entry
        self._fp.seek(offset)
        return self._fp.read(length).decode("utf-8", errors="replace")

    def get_many(self, docnos: list[str]) -> dict[str, str]:
        """Return {docno: text} for a list of docnos."""
        return {d: t for d in docnos if (t := self.get(d)) is not None}

_docstore = DocStore()


# ---------------------------------------------------------------------------
# Persistent summariser pool (PRD-008)
# ---------------------------------------------------------------------------

class SummarisePool:
    """
    Persistent subprocess running summarise.py.
    Accepts JSON lines on stdin, returns JSON lines on stdout.
    Falls back to pre-baked snippets on any error.
    """

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._req_counter = 0

    async def start(self):
        if not ZET_SUMMARISE:
            return
        if not os.path.exists(DOCSTORE_PATH):
            print("WARNING: ZET_SUMMARISE=1 but docstore not found — summariser disabled")
            return
        self._proc = await asyncio.create_subprocess_exec(
            "python3", SUMMARISE_PY,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        print(f"[summ_pool] summariser started (pid {self._proc.pid})", flush=True)

    async def summarise(self, query: str, docno_text: dict[str, str]) -> dict[str, str]:
        """
        Generate query-biased snippets. Returns {docno: snippet}.
        Falls back to empty dict on error (caller uses pre-baked snippets).
        """
        if not ZET_SUMMARISE or self._proc is None or not docno_text:
            return {}

        # Parse query terms (same light normalisation as summarise.py)
        terms = [t.lower().strip(string.punctuation)
                 for t in query.split()
                 if t.lower().strip(string.punctuation)]

        self._req_counter += 1
        req_id = str(self._req_counter)
        payload = json.dumps({
            "id": req_id,
            "terms": terms,
            "docs": docno_text,
        }, ensure_ascii=False)

        async with self._lock:
            try:
                self._proc.stdin.write((payload + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
                async with asyncio.timeout(SUMM_TIMEOUT):
                    raw = await self._proc.stdout.readline()
                resp = json.loads(raw.decode("utf-8", errors="replace"))
                return resp.get("summaries", {})
            except Exception as e:
                print(f"[summ_pool] error: {e} — falling back to pre-baked snippets", flush=True)
                # Respawn
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = await asyncio.create_subprocess_exec(
                    "python3", SUMMARISE_PY,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                return {}

    async def shutdown(self):
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.kill()
            except Exception:
                pass

_summ_pool = SummarisePool()


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
                    # Attach total/took_ms to first result as metadata carrier
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
        # Build env and args once
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
        # Should not reach here if semaphore is used correctly
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
            results = [r for r in raw if not r.get("_meta")][:n]  # slice to requested n

            return {
                "total": meta.get("total", len(results)),
                "took_ms": round(meta.get("took_ms", elapsed), 2),
                "results": results,
            }
        except Exception as e:
            # Worker may have crashed — respawn and surface error
            if not worker.is_alive():
                async with self._lock:
                    await self._respawn_worker(worker)
            raise e
        finally:
            worker.busy = False
            self._sem.release()

    async def shutdown(self):
        for w in self._workers:
            try:
                w.proc.stdin.close()
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
    # Startup
    await _load_sidecars()
    if ZET_SUMMARISE:
        _docstore.load()
    await _pool.start(ZET_WORKERS)
    await _summ_pool.start()
    yield
    # Shutdown
    await _pool.shutdown()
    await _summ_pool.shutdown()


app = FastAPI(title="Zettair Search Service", lifespan=lifespan)


async def _load_sidecars():
    global _snippets, _images, _autosuggest

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

    if os.path.exists(AUTOSUGGEST_PATH):
        print(f"Loading autosuggest from {AUTOSUGGEST_PATH}...", flush=True)
        with open(AUTOSUGGEST_PATH, encoding="utf-8") as f:
            _autosuggest = [tuple(x) for x in json.load(f)]
        print(f"  Loaded {len(_autosuggest):,} suggestions", flush=True)
    else:
        print(f"WARNING: autosuggest file not found: {AUTOSUGGEST_PATH}")


def enrich_results(results: list, qb_snippets: dict | None = None) -> list:
    """Attach snippets and images. Uses query-biased snippets when available."""
    enriched = []
    for r in results:
        docno = r.get("docno", "")
        # Prefer query-biased snippet; fall back to pre-baked
        if qb_snippets and docno in qb_snippets and qb_snippets[docno]:
            snippet = qb_snippets[docno]
        else:
            snippet = _snippets.get(docno, "")
        enriched.append({
            "rank": r["rank"],
            "score": r["score"],
            "docid": r["docid"],
            "docno": docno,
            "snippet": snippet,
            "image_url": _images.get(docno),
        })
    return enriched


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/suggest")
async def suggest(
    q: str = Query(..., description="Query prefix"),
    n: int = Query(8, ge=1, le=20),
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

    # PRD-008: generate query-biased snippets if enabled
    qb_snippets = {}
    if ZET_SUMMARISE and parsed["results"]:
        docnos = [r.get("docno", "") for r in parsed["results"] if r.get("docno")]
        docno_text = _docstore.get_many(docnos)
        if docno_text:
            qb_snippets = await _summ_pool.summarise(q.strip(), docno_text)

    results = enrich_results(parsed["results"], qb_snippets)

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
