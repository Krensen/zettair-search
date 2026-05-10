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
import urllib.parse
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
# PRD-017: per-field BM25 boosts. zet reads these env vars at startup and
# multiplies the term frequency contribution of each field-tagged occurrence.
# 1.0 = no boost; 3.0 is a reasonable starting point for title.
ZET_BOOST_TITLE    = os.environ.get("ZET_BOOST_TITLE",    "3.0")
ZET_BOOST_CAPTION  = os.environ.get("ZET_BOOST_CAPTION",  "1.0")
ZET_BOOST_CATEGORY = os.environ.get("ZET_BOOST_CATEGORY", "1.0")
ZET_BOOST_SEEALSO  = os.environ.get("ZET_BOOST_SEEALSO",  "1.0")
ZET_BOOST_INFOBOX  = os.environ.get("ZET_BOOST_INFOBOX",  "1.0")
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

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

def _client_ip(request: Request) -> tuple[str, bool]:
    """Return (ip, is_local).

    Caddy is the public-facing reverse proxy and sets X-Forwarded-For with
    the real client IP. The socket peer is always loopback for proxied
    traffic, so we cannot use it to identify the client. We trust
    X-Forwarded-For only when the socket peer IS loopback (i.e. the
    request really did come from Caddy on the same host); otherwise the
    header could be spoofed by something talking to :8765 directly.

    "local" means: a request that did NOT come through Caddy and arrived
    on loopback — i.e. curl/intent.py/loadtest.py running on the box.
    Those are excluded from /queries by default."""
    peer = (request.client.host if request.client else "") or ""
    if peer in _LOOPBACK:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
            return ip, False
        return peer, True
    return peer, False

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
                                    "took_ms": obj.get("took_ms", 0),
                                    "phases": {k: obj.get(k) for k in (
                                        "parse_ms", "eval_ms", "heap_ms",
                                        "summary_ms", "decode_ms", "walk_ms",
                                        "score_ms", "postings", "walk_steps")
                                        if k in obj}})
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
        # PRD-017: per-field BM25 boosts (always set; 1.0 = no boost)
        self._env["ZET_BOOST_TITLE"]    = ZET_BOOST_TITLE
        self._env["ZET_BOOST_CAPTION"]  = ZET_BOOST_CAPTION
        self._env["ZET_BOOST_CATEGORY"] = ZET_BOOST_CATEGORY
        self._env["ZET_BOOST_SEEALSO"]  = ZET_BOOST_SEEALSO
        self._env["ZET_BOOST_INFOBOX"]  = ZET_BOOST_INFOBOX

        # --b=0.0 disables BM25 length normalisation — long canonical
        # articles (e.g. Mark Zuckerberg) were losing to short related
        # articles (e.g. Randi Zuckerberg) on per-mention density.
        # k1/k3 kept at zet defaults so we only change the one knob.
        # No --summary= flag: PRD-016 replaced zet's C summariser with an
        # inline Python summariser that reads the cleaned docstore text.
        # Letting zet also build summaries was burning ~300ms on common-
        # term queries with no consumer for the output.
        self._args = [
            ZET_BINARY,
            "-f", ZET_INDEX,
            "--okapi",
            "--b=0.0",
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
                "phases": meta.get("phases", {}),
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


def enrich_results(results: list, query: str) -> tuple[list, dict]:
    """Attach url, snippet, and image. All sidecar stores are keyed by docno (safe_id).

    Snippet is generated by the inline Python summariser against the docstore.
    Falls back to the pre-baked snippets store if the docstore lookup misses
    or the summariser returns empty.

    Returns (enriched_results, timing_breakdown_ms).
    """
    t0 = time.perf_counter()
    query_terms = summarise.parse_query(query)
    enriched = []
    t_docstore = t_summarise = t_other = 0.0
    for r in results:
        docno = r.get("docno", "")
        snippet = ""
        ta = time.perf_counter()
        text = _docstore.get(docno)
        tb = time.perf_counter()
        t_docstore += (tb - ta)
        if text:
            snippet = summarise.summarise_doc(text, query_terms)
        tc = time.perf_counter()
        t_summarise += (tc - tb)
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
        td = time.perf_counter()
        t_other += (td - tc)
    timing = {
        "enrich_total_ms":  round((time.perf_counter() - t0) * 1000, 2),
        "docstore_ms":      round(t_docstore * 1000, 2),
        "summarise_ms":     round(t_summarise * 1000, 2),
        "other_stores_ms":  round(t_other * 1000, 2),
    }
    return enriched, timing


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
    request: Request,
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

    results, enrich_timing = enrich_results(parsed["results"], q.strip())

    ip, is_local = _client_ip(request)
    asyncio.create_task(_append_log(QUERY_LOG, {
        "ts": _ts(),
        "q": q.strip(),
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
        "ip": ip,
        "local": is_local,
    }))

    return {
        "query": q,
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
        "phases": parsed.get("phases", {}),
        "enrich": enrich_timing,
        "results": results,
    }


class ClickEvent(BaseModel):
    q: str
    docno: str
    rank: int
    score: float


@app.post("/click")
async def click(event: ClickEvent, request: Request):
    """Log a result click."""
    ip, is_local = _client_ip(request)
    asyncio.create_task(_append_log(CLICK_LOG, {
        "ts": _ts(),
        "q": event.q,
        "docno": event.docno,
        "rank": event.rank,
        "score": event.score,
        "ip": ip,
        "local": is_local,
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


@app.get("/queries", response_class=HTMLResponse)
async def queries_page(
    start: str = Query(None, description="UTC date YYYY-MM-DD (inclusive). Default: 1 day before end."),
    end: str = Query(None, description="UTC date YYYY-MM-DD (inclusive). Default: today (UTC)."),
    limit: int = Query(500, ge=1, le=10000, description="Max rows to render."),
    include_local: int = Query(0, description="1 = include localhost test traffic (curl, intent.py, loadtest.py)"),
    format: str = Query("html", regex="^(html|json)$"),
):
    """Aggregate the query log over a UTC date range, sorted by count."""
    today = datetime.datetime.utcnow().date()
    try:
        end_d = datetime.date.fromisoformat(end) if end else today
        start_d = datetime.date.fromisoformat(start) if start else (end_d - datetime.timedelta(days=1))
    except ValueError:
        return JSONResponse({"error": "start/end must be YYYY-MM-DD"}, status_code=400)
    if start_d > end_d:
        return JSONResponse({"error": "start must be <= end"}, status_code=400)

    start_iso = f"{start_d.isoformat()}T00:00:00Z"
    end_iso = f"{(end_d + datetime.timedelta(days=1)).isoformat()}T00:00:00Z"

    counts: dict[str, int] = {}
    total_queries = 0
    skipped_local = 0
    parse_errors = 0
    if os.path.exists(QUERY_LOG):
        try:
            with open(QUERY_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        parse_errors += 1
                        continue
                    ts = rec.get("ts", "")
                    if ts < start_iso or ts >= end_iso:
                        continue
                    if not include_local and rec.get("local"):
                        skipped_local += 1
                        continue
                    q = (rec.get("q") or "").strip()
                    if not q:
                        continue
                    counts[q] = counts.get(q, 0) + 1
                    total_queries += 1
        except Exception as e:
            return JSONResponse({"error": f"failed to read query log: {e}"}, status_code=500)

    rows = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]

    if format == "json":
        return JSONResponse({
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "total_queries": total_queries,
            "unique_queries": len(counts),
            "skipped_local": skipped_local,
            "include_local": bool(include_local),
            "rows": [{"q": q, "count": c} for q, c in rows],
        })

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))
    body_rows = "\n".join(
        f'<tr><td class="n">{i+1}</td>'
        f'<td class="c">{c}</td>'
        f'<td><a href="/?q={urllib.parse.quote(q)}">{esc(q)}</a></td></tr>'
        for i, (q, c) in enumerate(rows)
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Queries {esc(start_d.isoformat())} – {esc(end_d.isoformat())}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.4em; margin-bottom: 0.2em; }}
.summary {{ color: #666; margin-bottom: 1.5em; font-size: 0.9em; }}
form {{ margin-bottom: 1.5em; padding: 0.8em; background: #f6f6f6; border-radius: 4px; }}
form label {{ font-size: 0.9em; margin-right: 0.4em; }}
form input[type=date], form input[type=number] {{ padding: 0.3em; margin-right: 0.8em; font-size: 0.9em; }}
form button {{ padding: 0.3em 1em; font-size: 0.9em; cursor: pointer; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.92em; }}
th, td {{ text-align: left; padding: 0.35em 0.6em; border-bottom: 1px solid #eee; }}
th {{ background: #fafafa; font-weight: 600; }}
td.n {{ color: #999; width: 3em; text-align: right; }}
td.c {{ width: 5em; text-align: right; font-variant-numeric: tabular-nums; color: #444; }}
a {{ color: #1a5fb4; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>Queries {esc(start_d.isoformat())} – {esc(end_d.isoformat())}</h1>
<div class="summary">{total_queries:,} total queries, {len(counts):,} unique. Showing top {len(rows):,}.{(' ' + f'{skipped_local:,} localhost test queries excluded.') if skipped_local else ''}{(' ' + str(parse_errors) + ' malformed log lines skipped.') if parse_errors else ''}</div>
<form method="get" action="/queries">
  <label>Start <input type="date" name="start" value="{esc(start_d.isoformat())}"></label>
  <label>End <input type="date" name="end" value="{esc(end_d.isoformat())}"></label>
  <label>Limit <input type="number" name="limit" value="{limit}" min="1" max="10000" style="width:6em"></label>
  <label><input type="checkbox" name="include_local" value="1"{' checked' if include_local else ''}> include localhost</label>
  <button type="submit">Apply</button>
  <a href="/queries?start={esc(start_d.isoformat())}&amp;end={esc(end_d.isoformat())}&amp;limit={limit}&amp;include_local={1 if include_local else 0}&amp;format=json" style="margin-left:1em">JSON</a>
</form>
<table>
<thead><tr><th class="n">#</th><th class="c">count</th><th>query</th></tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</body></html>"""
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def index():
    return _index_html


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ZET_PORT)
