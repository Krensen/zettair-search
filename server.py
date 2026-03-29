"""
Zettair Search Web Service
FastAPI wrapper around the zet CLI with async queuing.
"""
import asyncio
import os
import re
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- Config (env vars with sensible defaults) ---
ZET_BINARY = os.environ.get(
    "ZET_BINARY",
    os.path.join(os.path.dirname(__file__), "../zettair/devel/zet"),
)
ZET_INDEX = os.environ.get(
    "ZET_INDEX",
    os.path.join(os.path.dirname(__file__), "../zettair/testindex/index"),
)
ZET_PORT = int(os.environ.get("ZET_PORT", "8765"))

app = FastAPI(title="Zettair Search Service")

# Serialize access to the zet subprocess
_lock = asyncio.Lock()


def parse_zet_output(output: str) -> dict:
    """
    Parse zet stdout into structured results.

    Example output:
      > 1. Chapter 36, Paragraph 25 (score 5.959397, docid 773)
      > "It's a white whale..."
      > 2. Chapter 131, Paragraph 4 (score 5.547001, docid 2460)
      > "Hast seen the White Whale?"
      > 25 results of 852 shown (took 0.000617 seconds)
      > > 677 microseconds querying (excluding loading/unloading)
    """
    results = []
    total = 0
    took_ms = None

    # Regex for result header line
    header_re = re.compile(
        r"^(\d+)\.\s+(.*?)\s+\(score\s+([\d.]+),\s+docid\s+(\d+)\)"
    )
    # Regex for summary line
    summary_re = re.compile(r"^(\d+) results of (\d+) shown \(took ([\d.]+) seconds\)")

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        # Strip the "> " prompt prefix that appears on the first result and summary lines
        line = raw[2:] if raw.startswith("> ") else raw
        line = line.strip()

        m = header_re.match(line)
        if m:
            rank = int(m.group(1))
            title = m.group(2).strip()
            score = float(m.group(3))
            docid = int(m.group(4))
            # Next line is the snippet (no prefix)
            snippet = ""
            if i + 1 < len(lines):
                snippet = lines[i + 1].strip().strip('"')
                i += 1
            results.append(
                {
                    "rank": rank,
                    "score": score,
                    "docid": docid,
                    "title": title,
                    "snippet": snippet,
                }
            )
            i += 1
            continue

        m = summary_re.match(line)
        if m:
            total = int(m.group(2))
            took_ms = round(float(m.group(3)) * 1000, 3)

        i += 1

    return {"total": total, "took_ms": took_ms, "results": results}


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
    return {
        "query": q,
        "total": parsed["total"],
        "took_ms": parsed["took_ms"],
        "results": parsed["results"],
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ZET_PORT)
