# PRD-007 — Persistent Zettair Process + JSON Output

**Status:** Draft  
**Created:** 2026-03-30

---

## Problem

Every search query currently:

1. Forks a new `zet` subprocess
2. Loads the full index from disk (~100–200ms startup overhead)
3. Runs the query
4. Prints results to stdout
5. Exits — discarding all loaded state

This means the index is loaded and discarded on every single query. Under any real load, the serialisation lock in `server.py` creates a bottleneck, and latency is dominated by process startup rather than actual search time.

Zettair already supports persistent interactive mode — it prints a `>` prompt, waits for a query, returns results, and loops. We're not using this.

---

## Goals

1. **Keep one (or more) Zettair processes alive** across queries — index loaded once at startup
2. **Add `--output=json` to Zettair** so responses are easy to parse without brittle text parsing
3. **Maintain the existing API contract** — `/search` endpoint behaviour unchanged from the caller's perspective
4. **Handle process death gracefully** — auto-respawn if Zettair crashes

---

## Non-Goals

- Multi-shard aggregation (future PRD)
- Full Zettair daemon / socket server (future PRD)
- Connection pooling beyond a simple round-robin worker pool

---

## Design

### Part 1 — Zettair JSON output mode

Add `--output=json` flag to `commandline.c`. When set, each result is emitted as a JSON object on a single line, followed by a sentinel to mark end-of-results:

```
{"rank":1,"docno":"Albert_Einstein","score":36.34,"docid":523}
{"rank":2,"docno":"Theory_of_relativity","score":35.12,"docid":10662}
{"done":true,"count":5,"total":2580,"took_ms":2.1}
```

Rules:
- One JSON object per line (NDJSON / JSON Lines format)
- `{"done":true,...}` sentinel is always the last line for a query response
- `score` is a float rounded to 2 decimal places
- `docid` is the internal Zettair document number (useful for click prior debugging)
- `took_ms` is query execution time in milliseconds
- The interactive `>` prompt is still printed to stderr (not stdout) so it doesn't interfere with stdout parsing
- `--output=plain` (default) retains current behaviour unchanged

### Part 2 — Persistent process pool in server.py

Replace `asyncio.create_subprocess_exec` (per-query) with a small pool of long-lived Zettair processes.

**Pool size:** configurable via `ZET_WORKERS` env var, default 2.

**Protocol:**
```
[server] → write query + "\n" to stdin
[zettair] → write N result lines + sentinel line to stdout
[server] → read lines until {"done":true} sentinel
```

**Startup:**
- Pool initialised at FastAPI `lifespan` startup
- Each worker process started with `--output=json` and all existing flags
- Workers marked `available=True` initially

**Per-query:**
```python
worker = await pool.acquire()   # blocks until a worker is free
worker.stdin.write(query + "\n")
lines = await read_until_sentinel(worker.stdout)
pool.release(worker)
results = [json.loads(l) for l in lines if not l.startswith('{"done"')]
```

**Worker death / respawn:**
- If `worker.returncode is not None` (process died), respawn before returning it to pool
- Log the crash to `logs/zet_crashes.jsonl`
- Respawn limit: 5 per minute before raising an alert

**Timeout:**
- Per-query timeout: 5 seconds (configurable via `ZET_QUERY_TIMEOUT`)
- On timeout: kill worker, respawn, return 504 to caller

### Part 3 — Remove the serialisation lock

The current `_lock` in `server.py` serialises all queries through a single Zettair process. With a pool, remove it — each worker handles one query at a time, concurrency = pool size.

---

## Implementation Plan

### Step 1 — Patch `okapi.c` / `search.c` for JSON output

Locate where results are printed in `commandline.c` (the `--query` handler loop). Add a branch:

```c
if (output_json) {
    printf("{\"rank\":%d,\"docno\":\"%s\",\"score\":%.2f,\"docid\":%lu}\n",
           rank, docno_str, score, docno);
} else {
    /* existing plain text output */
}
```

After all results:
```c
if (output_json) {
    printf("{\"done\":true,\"count\":%d,\"total\":%lu,\"took_ms\":%.1f}\n",
           count, total, took_ms);
}
```

Move the `>` prompt print from stdout to stderr.

### Step 2 — Add `--output` flag to argument parser

In `getlongopt` / argument parsing section of `commandline.c`:
```c
{ "output", GETLONGOPT_ARG_REQUIRED, 'O' }
```

Set `output_json = (strcmp(optarg, "json") == 0)`.

### Step 3 — Rewrite `run_query()` in `server.py`

Replace subprocess-per-query with pool acquire/release. Key changes:

```python
class ZetWorker:
    proc: asyncio.subprocess.Process
    busy: bool = False

class ZetPool:
    workers: list[ZetWorker]
    
    async def acquire(self) -> ZetWorker: ...
    async def release(self, w: ZetWorker): ...
    async def respawn(self, w: ZetWorker): ...

async def run_query(query: str, n: int) -> list[dict]:
    worker = await pool.acquire()
    try:
        worker.proc.stdin.write((query + "\n").encode())
        await worker.proc.stdin.drain()
        results = []
        async with asyncio.timeout(ZET_QUERY_TIMEOUT):
            async for line in worker.proc.stdout:
                obj = json.loads(line)
                if obj.get("done"):
                    break
                results.append(obj)
        return results
    except Exception:
        await pool.respawn(worker)
        raise
    finally:
        pool.release(worker)
```

### Step 4 — Update startup / shutdown

```python
@asynccontextmanager
async def lifespan(app):
    await pool.start(size=ZET_WORKERS)
    yield
    await pool.shutdown()
```

### Step 5 — Remove `_lock`, update logging

Remove the `async with _lock` guard. Log worker pool stats to startup output.

---

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `ZET_WORKERS` | `2` | Number of persistent Zettair workers |
| `ZET_QUERY_TIMEOUT` | `5.0` | Per-query timeout in seconds |

---

## Test Plan

| ID | Test | Expected |
|----|------|----------|
| T1 | Start server, check logs show N workers started | `[zet_pool] started 2 workers` |
| T2 | `GET /search?q=london` | Returns results, London #1 |
| T3 | Run 20 concurrent requests | All return results, no errors, no lock contention |
| T4 | Kill one worker process manually (`kill -9 <pid>`) | Worker respawns, subsequent queries succeed |
| T5 | Set `ZET_WORKERS=4`, restart, run load test | 4 workers visible in process list |
| T6 | Query with `--output=json` directly on CLI | Valid NDJSON, sentinel present |
| T7 | Compare T2 results vs current server (plain output) | Identical ranking |
| T8 | Check latency improvement | p50 latency < 20ms (vs ~200ms current) |

---

## Expected Outcome

- Query latency: **~2ms** (down from ~200ms) — index loaded once, stays hot
- Throughput: **~500 QPS** on a single box with 4 workers (vs ~5 QPS current)
- Reliability: worker crash → auto-respawn, no user-visible downtime
- Code: simpler result parsing (JSON vs regex on plain text)

---

## Future Work

- **PRD-008:** Unix socket interface — replace stdin/stdout with a socket, enabling cross-machine aggregation for sharding
- **PRD-009:** Full English Wikipedia migration — larger corpus, same architecture
