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
ZET_WORKERS      = int(os.environ.get("ZET_WORKERS", "4"))
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
# PRD-018: knowledge-panel summaries. Keyed by query_norm, generated offline.
SUMMARIES_STORE_PATH = os.environ.get("ZET_SUMMARIES_STORE", os.path.join(_wiki_dir, "summaries.store"))
SUMMARIES_MAP_PATH   = os.environ.get("ZET_SUMMARIES_MAP",   os.path.join(_wiki_dir, "summaries.map"))
# PRD-020: trending pages. Written by tools/fetch_trending.py on a timer.
TRENDING_CURRENT_PATH = os.environ.get("ZET_TRENDING_CURRENT", "/mnt/wikipedia-source/trending/current.json")
# PRD-025: related entities. Built offline at index-rebuild time by
# zettair/wikipedia/build_related.py. FlatStore keyed by docno; value
# is a JSON array of [target_docno, score] pairs.
RELATED_STORE_PATH = os.environ.get("ZET_RELATED_STORE", os.path.join(_wiki_dir, "related.store"))
RELATED_MAP_PATH   = os.environ.get("ZET_RELATED_MAP",   os.path.join(_wiki_dir, "related.map"))
# Per-docno class label so the frontend can render a class-aware rail
# header ("Related people" / "Related places" / etc).
RELATED_CLASS_PATH = os.environ.get("ZET_RELATED_CLASS", "/mnt/wikipedia-source/related/entity_class.json")
# PRD-027: reading-time + difficulty sidecar. Packed binary written by
# tools/build_reading_sidecar.py; loaded into RAM at startup.
READING_SIDECAR_PATH = os.environ.get("ZET_READING_SIDECAR",
                                      os.path.join(_wiki_dir, "enwiki_top1m.reading.bin"))

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


_snippets_store  = FlatStore(SNIPPETS_STORE_PATH,  SNIPPETS_MAP_PATH,  "snippets")
_images_store    = FlatStore(IMAGES_STORE_PATH,    IMAGES_MAP_PATH,    "images")
_urls_store      = FlatStore(URLS_STORE_PATH,      URLS_MAP_PATH,      "urls")
_summaries_store = FlatStore(SUMMARIES_STORE_PATH, SUMMARIES_MAP_PATH, "summaries")
_related_store   = FlatStore(RELATED_STORE_PATH,   RELATED_MAP_PATH,   "related")
_related_class:  dict = {}   # PRD-025 docno -> class label, loaded at startup

# PRD-027: docno -> reading_time_min and docno -> difficulty.
# Two parallel dicts so each lookup is one hash. Missing docno => no pills.
_reading_time: dict[str, int] = {}
_difficulty:   dict[str, str] = {}
_DIFF_CODE_TO_LABEL = {1: "accessible", 2: "moderate", 3: "technical"}


def _load_reading_sidecar() -> None:
    """Load the PRD-027 packed binary into _reading_time / _difficulty.
    Missing file is fine — the rail just doesn't render those pills."""
    import struct
    if not os.path.exists(READING_SIDECAR_PATH):
        print(f"WARNING: {READING_SIDECAR_PATH} not found — reading-time pills disabled", flush=True)
        return
    try:
        with open(READING_SIDECAR_PATH, "rb") as f:
            magic = f.read(4)
            if magic != b"RDT1":
                print(f"WARNING: {READING_SIDECAR_PATH} bad magic {magic!r} — skipping", flush=True)
                return
            (n,) = struct.unpack("<I", f.read(4))
            blob = f.read()
        # One-shot parse of the body. Each entry: u16 len, len bytes, u16 rt, u8 diff.
        pos = 0
        for _ in range(n):
            (length,) = struct.unpack_from("<H", blob, pos); pos += 2
            docno = blob[pos:pos + length].decode("utf-8", errors="replace"); pos += length
            rt, diff_code = struct.unpack_from("<HB", blob, pos); pos += 3
            _reading_time[docno] = rt
            label = _DIFF_CODE_TO_LABEL.get(diff_code)
            if label is not None:
                _difficulty[docno] = label
        print(f"  reading-sidecar: {len(_reading_time):,} entries "
              f"({len(_difficulty):,} with difficulty)", flush=True)
    except (OSError, struct.error) as e:
        print(f"WARNING: couldn't load {READING_SIDECAR_PATH}: {e}", flush=True)


def _load_related_classes() -> None:
    """Load entity_class.json into _related_class (mutated in place).
    Missing file is fine — feature degrades to "no class on the rail
    header"."""
    if not os.path.exists(RELATED_CLASS_PATH):
        print(f"WARNING: {RELATED_CLASS_PATH} not found — related-class headers disabled", flush=True)
        return
    try:
        with open(RELATED_CLASS_PATH, encoding="utf-8") as f:
            _related_class.update(json.load(f))
        print(f"  related-class: {len(_related_class):,} entries", flush=True)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: couldn't load {RELATED_CLASS_PATH}: {e}", flush=True)


def _related_for(docno: str, n: int = 8) -> tuple[list[dict], str | None]:
    """Return (items, source_class). Items is up to `n` dicts each
    with docno + title + score. Empty list if no related data."""
    if not docno:
        return [], None
    blob = _related_store.get(docno)
    if not blob:
        return [], None
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError:
        return [], None
    src_class = _related_class.get(docno)
    items = []
    for entry in raw[:n]:
        try:
            t_docno, score = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        items.append({
            "docno": t_docno,
            "title": t_docno.replace("_", " "),
            "score": score,
        })
    return items, src_class

# PRD-018: shared normalisation for summary lookups. Same function must
# be used by the offline summary generator and the live server.
def query_norm(s: str) -> str:
    return " ".join(s.lower().strip().split())
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

        # Spawn workers concurrently. Each worker independently mmaps the
        # index, click prior, and field-lengths sidecar at startup, so
        # serial spawn made startup cost (sidecars × workers) when it
        # only had to be (sidecars × 1) walltime.
        self._workers = list(await asyncio.gather(
            *(self._spawn(i) for i in range(size))
        ))

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
    _summaries_store.load()
    _related_store.load()
    _load_related_classes()
    _load_reading_sidecar()
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
    _summaries_store.close()
    _related_store.close()
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
            # PRD-027: reading-time + difficulty. Either may be absent
            # (sidecar not built, or article too short for difficulty);
            # frontend hides the pill when null.
            "reading_time_min": _reading_time.get(docno),
            "difficulty": _difficulty.get(docno),
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

    # PRD-018: knowledge-panel summary. Lookup is keyed by normalised
    # query (lowercase + collapsed whitespace). Missing → field absent.
    # PRD-021: if the query is currently spiking AND we have a news
    # summary for it, prefer that. Falls through to biographical
    # otherwise. summary_kind tells the frontend which badge to render.
    qn = query_norm(q)
    summary = None
    summary_kind = None
    event_date = None
    spike_meta = _trending_spike_meta(qn)
    if spike_meta is not None:
        news = _summaries_store.get(f"{qn}:news")
        if news:
            summary = news
            summary_kind = "news"
            event_date = spike_meta.get("event_date")
    if summary is None:
        bio = _summaries_store.get(qn)
        if bio:
            summary = bio
            summary_kind = "biographical"

    response = {
        "query": q,
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
        "phases": parsed.get("phases", {}),
        "enrich": enrich_timing,
        "results": results,
    }
    if summary:
        response["summary"] = summary
        response["summary_kind"] = summary_kind
        if event_date:
            response["event_date"] = event_date

    # PRD-025: related entities for the top result, if any.
    if results:
        top_docno = results[0].get("docno")
        related_items, related_class = _related_for(top_docno, n=8)
        if related_items:
            response["related"] = {
                "source_class": related_class,
                "items": related_items,
            }
    return response


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


# --- PRD-020: trending pages --------------------------------------------
#
# The fetcher writes current.json on a timer. We cache the parsed
# payload in memory and only re-read when the file's mtime changes.
# A missing file returns {"items": []} so the chip rail hides itself
# rather than the homepage breaking.

_trending_cache: dict = {"mtime": 0.0, "payload": {"mode": "raw", "items": []}}


def _read_trending() -> dict:
    try:
        st = os.stat(TRENDING_CURRENT_PATH)
    except FileNotFoundError:
        return {"mode": "raw", "items": []}
    if st.st_mtime != _trending_cache["mtime"]:
        try:
            with open(TRENDING_CURRENT_PATH, "rb") as f:
                _trending_cache["payload"] = json.load(f)
            _trending_cache["mtime"] = st.st_mtime
        except (json.JSONDecodeError, OSError):
            # Don't poison the cache on a transient bad read; return last good.
            return _trending_cache["payload"]
    return _trending_cache["payload"]


# PRD-021: don't serve a news summary whose paragraph is older than
# this. Must be >= the producer's EVENT_FRESHNESS_DAYS (currently 30,
# tools/fetch_trending.py) — otherwise the producer generates summaries
# the server immediately refuses to serve. 30 keeps us in sync; if we
# later tighten the producer back to 14d, tighten this too.
STALE_NEWS_DAYS_SERVE = 30

# Grace window after a query drops off the spike rail. News spikes are
# bursty — articles dip below the spike threshold for a sample or two
# (especially overnight UTC) and reappear. Without this window the
# news panel would flicker. 24 hours = clearly "still recent news" to
# a human; longer would risk serving stale news for a story that ended.
SPIKE_GRACE_HOURS = 24

TRENDING_RECENTLY_SEEN_PATH = os.environ.get(
    "ZET_TRENDING_RECENTLY_SEEN",
    "/mnt/wikipedia-source/trending/recently_seen.json",
)

# Mtime-cached reader for the recently-seen file. Same pattern as
# _trending_cache; the file is rewritten every 3h by the trending
# fetcher, so the cache stays fresh between fetches at zero cost.
_recently_seen_cache: dict = {"mtime": 0.0, "data": {}}


def _read_recently_seen() -> dict:
    try:
        st = os.stat(TRENDING_RECENTLY_SEEN_PATH)
    except FileNotFoundError:
        return {}
    if st.st_mtime != _recently_seen_cache["mtime"]:
        try:
            with open(TRENDING_RECENTLY_SEEN_PATH, "rb") as f:
                _recently_seen_cache["data"] = json.load(f)
            _recently_seen_cache["mtime"] = st.st_mtime
        except (json.JSONDecodeError, OSError):
            return _recently_seen_cache["data"]
    return _recently_seen_cache["data"]


def _seen_within_grace(query_norm_str: str) -> bool:
    """True if the query was on the spike rail within the last
    SPIKE_GRACE_HOURS."""
    seen = _read_recently_seen()
    ts = seen.get(query_norm_str)
    if not ts:
        return False
    try:
        last = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    age_hrs = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 3600.0
    return age_hrs <= SPIKE_GRACE_HOURS


def _trending_spike_meta(query_norm_str: str) -> dict | None:
    """Return the trending item (with event_date / event_paragraph) if
    the query is currently spiking OR was recently spiking within the
    grace window, AND the event is still fresh. Otherwise None —
    caller falls through to biographical summary.

    The grace window prevents the news panel from flickering when a
    query drops off the spike rail for a single sample (the rail
    recomputes every 3h and traffic is bursty)."""
    payload = _read_trending()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    # Path 1: currently on the spike rail with full item metadata.
    if payload.get("mode") == "spike":
        for it in payload.get("items", []):
            if it.get("query", "").strip().lower() != query_norm_str:
                continue
            ed = it.get("event_date")
            if not ed:
                return it
            try:
                event_date = datetime.date.fromisoformat(ed)
            except ValueError:
                return None
            if (today - event_date).days > STALE_NEWS_DAYS_SERVE:
                return None
            return it
    # Path 2: recently-on-the-rail grace window. We don't have the full
    # item metadata so we return a shell dict with whatever we know.
    # The caller checks for the :news summary's existence and serves it
    # if so. event_date can't be enforced here because we don't store
    # it in recently_seen.json — STALE_NEWS_DAYS_SERVE is enforced by
    # the producer (it stops generating beyond EVENT_FRESHNESS_DAYS)
    # and by the grace window naturally bounding "recently spiking".
    if _seen_within_grace(query_norm_str):
        return {"query": query_norm_str, "grace_window": True}
    return None


@app.get("/api/trending")
async def trending(n: int = Query(8, ge=1, le=50)):
    """PRD-020: return the current trending list (chip-rail data).

    For each trending article we check whether its docno is in our
    corpus. If yes the chip behaves as a search query; if no it's a
    direct link to en.wikipedia.org so the user still gets to the
    article. Two visual styles on the frontend, one data flow here.
    """
    payload = _read_trending()
    items = payload.get("items", [])[:n]
    out = []
    for it in items:
        if not it.get("query"):
            continue
        # Newer payloads carry the raw url-form title as `docno`.
        # Older payloads (or anything where the fetcher hasn't run
        # since the docno field was added) won't. Reconstruct from
        # the display title in that case — spaces back to underscores
        # is the canonical wikipedia url form for ~all titles.
        docno = it.get("docno")
        if not docno and it.get("title"):
            docno = it["title"].replace(" ", "_")
        in_index = bool(docno) and docno in _docstore._map
        entry = {
            "query": it["query"],
            "title": it["title"],
            "in_index": in_index,
            # PRD-026: which source surfaced this chip. Used by the
            # frontend for source-aware labels/icons if desired. Items
            # written before PRD-026 land here as "spike" by default.
            "source": it.get("source", "spike"),
        }
        if not in_index and docno:
            entry["wiki_url"] = f"https://en.wikipedia.org/wiki/{docno}"
        out.append(entry)
    return {
        "mode": payload.get("mode", "raw"),
        "generated_at": payload.get("generated_at"),
        "items": out,
    }


@app.get("/img")
async def image_proxy(url: str = Query(...)):
    """Proxy Wikimedia images to avoid browser-side rate limiting."""
    if not url.startswith("https://upload.wikimedia.org/"):
        return Response(status_code=403)
    # urllib.request can't handle non-ASCII characters in URLs (it does
    # not percent-encode them automatically). Wikimedia commons URLs
    # frequently contain non-ASCII path segments — "Andrés_Iniesta",
    # "FC_Barcelona_Femení", anything with diacritics. Percent-encode
    # the path + query while leaving the scheme + host alone.
    try:
        parsed = urllib.parse.urlsplit(url)
        safe_path = urllib.parse.quote(parsed.path, safe="/")
        safe_query = urllib.parse.quote(parsed.query, safe="=&")
        safe_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, safe_path, safe_query, "")
        )
    except Exception:
        return Response(status_code=400)
    try:
        # Run blocking urllib call in a thread so the event loop isn't blocked.
        # Wikimedia's anti-abuse layer 400s requests whose User-Agent doesn't
        # include a contact (email or URL where they can reach the operator).
        # Without it they return "Use thumbnail steps listed on …" — a
        # misleading error that has nothing to do with the actual problem.
        req = urllib.request.Request(safe_url, headers={
            "User-Agent": "ZettairSearch/1.0 (https://zettair.io; hugh@viaaltoadvisors.com)",
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
