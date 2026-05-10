#!/usr/bin/env python3
"""
install_summaries.py — PRD-018 installer.

Drains <summaries>/done/*.md into the FlatStore (via summaries_admin.py
add semantics inlined here for performance), moves drained files to
installed/, and restarts the zettair-search service if anything new
landed.

Acquires a flock on <summaries>/installer.lock so concurrent installer
runs and the manual summaries_admin.py don't clobber each other.

Runs as the `zettair` user via systemd timer (every ~5 min).
Has NOPASSWD for `/bin/systemctl restart zettair-search` via
/etc/sudoers.d/zettair-installer (installed by setup.sh).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


SUMMARIES_DIR = Path(os.environ.get(
    "ZET_SUMMARIES_DIR",
    "/mnt/wikipedia-source/summaries",
))
SUMMARIES_STORE = Path(os.environ.get(
    "ZET_SUMMARIES_STORE",
    "/mnt/wikipedia-source/summaries.store",
))
SUMMARIES_MAP = Path(os.environ.get(
    "ZET_SUMMARIES_MAP",
    "/mnt/wikipedia-source/summaries.map",
))


def query_norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_map() -> dict:
    if not SUMMARIES_MAP.exists():
        return {}
    with open(SUMMARIES_MAP, encoding="utf-8") as f:
        return json.load(f)


def write_map(m: dict) -> None:
    tmp = SUMMARIES_MAP.with_suffix(".map.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, SUMMARIES_MAP)


def append_to_store(blob: bytes) -> tuple[int, int]:
    """Append blob to summaries.store. Returns (offset, length).

    Note: the FlatStore only grows. summaries_admin.py build does a
    full rewrite when called from scratch; that's the GC path. The
    installer just appends, leaving orphan bytes when a query gets
    re-summarised (the new offset shadows the old map entry). At
    millions-of-summaries scale we'd want a periodic compaction job;
    at thousands we don't bother."""
    SUMMARIES_STORE.parent.mkdir(parents=True, exist_ok=True)
    if not SUMMARIES_STORE.exists():
        open(SUMMARIES_STORE, "wb").close()
    with open(SUMMARIES_STORE, "ab") as f:
        offset = f.tell()
        f.write(blob)
    return offset, len(blob)


def restart_service() -> None:
    log("restarting zettair-search to reload the offset map...")
    r = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "restart", "zettair-search"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log(f"  restart failed rc={r.returncode}: {r.stderr.strip()}")
        log(f"  (need /etc/sudoers.d/zettair-installer for NOPASSWD systemctl)")
    else:
        log("  restart ok")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-restart", action="store_true",
                   help="install but don't restart the service (debug)")
    p.add_argument("--batch-size", type=int, default=200,
                   help="max files to drain per run [default 200]")
    args = p.parse_args()

    done_dir = SUMMARIES_DIR / "done"
    installed_dir = SUMMARIES_DIR / "installed"
    lock_path = SUMMARIES_DIR / "installer.lock"

    if not done_dir.exists():
        log(f"{done_dir} does not exist; nothing to install")
        return

    installed_dir.mkdir(parents=True, exist_ok=True)

    # Single-instance flock for the duration of the batch.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"another install_summaries is running (lock {lock_path} held); exiting")
            return

        files = sorted(done_dir.glob("*.md"))[: args.batch_size]
        if not files:
            log("no done/*.md to install")
            return

        log(f"installing {len(files)} summary file(s)...")
        m = load_map()
        n_added = n_skipped = n_errors = 0
        for f in files:
            qnorm = query_norm(f.stem)
            try:
                body = f.read_text(encoding="utf-8")
            except Exception as e:
                log(f"  read failed for {f.name}: {e}")
                n_errors += 1
                continue
            body_stripped = body.strip()
            if not body_stripped:
                log(f"  skip empty {f.name}")
                n_skipped += 1
                # Move out of done/ anyway so it doesn't get retried forever
                f.replace(installed_dir / f.name)
                continue
            blob = body_stripped.encode("utf-8")
            offset, length = append_to_store(blob)
            m[qnorm] = [offset, length]
            f.replace(installed_dir / f.name)
            n_added += 1

        if n_added:
            write_map(m)

        log(
            f"done: added={n_added} skipped_empty={n_skipped} "
            f"errors={n_errors} total_map_size={len(m):,}"
        )

        if n_added and not args.no_restart:
            restart_service()
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
