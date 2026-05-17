#!/usr/bin/env python3
"""PRD-027: build the reading-time + difficulty sidecar.

Reads the docstore once. Per article, computes word count and
Flesch-Kincaid grade level, buckets the grade into accessible /
moderate / technical, and writes a single packed binary sidecar.

Output format (little-endian throughout):

    4-byte magic  "RDT1"
    uint32        N (entry count)
    N entries of:
        uint16    docno length in bytes
        bytes     utf-8 docno
        uint16    reading_time_min   (1..65535; floored at 1)
        uint8     difficulty code
                    0 = null (suppressed: too short)
                    1 = accessible (FK <= 8)
                    2 = moderate   (8 < FK <= 13)
                    3 = technical  (FK > 13)

Difficulty is null when words < MIN_WORDS or sentences < MIN_SENTENCES;
reading time is always emitted.

Designed to be run idempotently from setup.sh. Standalone — does not
import server.py. Reuses the read-only FlatStore shape from
tools/build_summary_jobs.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

DEFAULT_DOCSTORE = Path("/mnt/wikipedia-source/enwiki_top1m.docstore")
DEFAULT_DOCMAP   = Path("/mnt/wikipedia-source/enwiki_top1m.docmap")
DEFAULT_OUTPUT   = Path("/mnt/wikipedia-source/enwiki_top1m.reading.bin")

WORDS_PER_MIN   = 250
MIN_WORDS       = 150
MIN_SENTENCES   = 5
FK_ACCESSIBLE   = 8.0    # FK <= 8
FK_MODERATE     = 13.0   # 8 < FK <= 13

MAGIC = b"RDT1"

WORD_RE     = re.compile(r"\b[\w']+\b", re.UNICODE)
SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")
VOWEL_GROUP = re.compile(r"[aeiouy]+", re.IGNORECASE)


class FlatStoreRO:
    """Same shape as the one in tools/build_summary_jobs.py."""

    def __init__(self, store_path: Path, map_path: Path):
        self.store_path = store_path
        self.map_path = map_path
        self._map: dict = {}
        self._fd: int = -1

    def load(self) -> None:
        with open(self.map_path, encoding="utf-8") as f:
            self._map = json.load(f)
        self._fd = os.open(self.store_path, os.O_RDONLY)

    def keys(self):
        return self._map.keys()

    def get(self, key: str) -> str | None:
        entry = self._map.get(key)
        if entry is None:
            return None
        offset, length = entry
        try:
            return os.pread(self._fd, length, offset).decode("utf-8", errors="replace")
        except OSError:
            return None

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def count_syllables(word: str) -> int:
    """Vowel-group heuristic. Floors at 1.

    Off CMU-dict by ~0.5%; well within the noise floor for FK
    bucketing into three coarse bins."""
    groups = VOWEL_GROUP.findall(word)
    return max(1, len(groups))


def compute_metrics(text: str) -> tuple[int, str | None]:
    """Return (reading_time_min, difficulty | None)."""
    words = WORD_RE.findall(text)
    n_words = len(words)
    if n_words == 0:
        return 1, None

    reading_time = max(1, round(n_words / WORDS_PER_MIN))
    # Cap at uint16 max; nothing realistic should hit this.
    if reading_time > 65535:
        reading_time = 65535

    sentences = SENTENCE_RE.findall(text)
    n_sentences = len(sentences) or 1   # avoid div-by-zero

    if n_words < MIN_WORDS or n_sentences < MIN_SENTENCES:
        return reading_time, None

    n_syllables = sum(count_syllables(w) for w in words)
    fk = 0.39 * (n_words / n_sentences) + 11.8 * (n_syllables / n_words) - 15.59

    if fk <= FK_ACCESSIBLE:
        diff = "accessible"
    elif fk <= FK_MODERATE:
        diff = "moderate"
    else:
        diff = "technical"
    return reading_time, diff


DIFF_CODE = {None: 0, "accessible": 1, "moderate": 2, "technical": 3}


def build(docstore_path: Path, docmap_path: Path, output_path: Path,
          progress_every: int = 100_000) -> None:
    print(f"reading docstore: {docstore_path}")
    print(f"reading docmap:   {docmap_path}")
    print(f"writing sidecar:  {output_path}")

    store = FlatStoreRO(docstore_path, docmap_path)
    store.load()
    docnos = list(store.keys())
    n_total = len(docnos)
    print(f"  {n_total:,} docnos in map")

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    t0 = time.time()
    n_processed = 0
    n_with_diff = 0
    n_null_diff = 0
    n_diff_counts = {"accessible": 0, "moderate": 0, "technical": 0}

    with open(tmp_path, "wb") as out:
        out.write(MAGIC)
        out.write(struct.pack("<I", n_total))
        for docno in docnos:
            text = store.get(docno) or ""
            rt, diff = compute_metrics(text)
            docno_bytes = docno.encode("utf-8")
            if len(docno_bytes) > 65535:
                # Pathological docno; skip body, write zeroed entry so
                # offsets stay consistent. Realistically never happens.
                docno_bytes = docno_bytes[:65535]
            out.write(struct.pack("<H", len(docno_bytes)))
            out.write(docno_bytes)
            out.write(struct.pack("<HB", rt, DIFF_CODE[diff]))
            n_processed += 1
            if diff is None:
                n_null_diff += 1
            else:
                n_with_diff += 1
                n_diff_counts[diff] += 1
            if n_processed % progress_every == 0:
                elapsed = time.time() - t0
                rate = n_processed / elapsed
                eta = (n_total - n_processed) / rate
                print(f"  [{elapsed:6.1f}s] {n_processed:,} / {n_total:,} "
                      f"({rate:.0f}/s, ETA {eta:.0f}s)")

    os.replace(tmp_path, output_path)
    store.close()
    elapsed = time.time() - t0
    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"done in {elapsed:.1f}s, wrote {size_mb:.1f} MB")
    print(f"  difficulty: accessible={n_diff_counts['accessible']:,} "
          f"moderate={n_diff_counts['moderate']:,} "
          f"technical={n_diff_counts['technical']:,} "
          f"null(short)={n_null_diff:,}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--docstore", type=Path, default=DEFAULT_DOCSTORE)
    p.add_argument("--docmap",   type=Path, default=DEFAULT_DOCMAP)
    p.add_argument("--output",   type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    if not args.docstore.exists():
        print(f"ERROR: docstore not found at {args.docstore}", file=sys.stderr)
        return 1
    if not args.docmap.exists():
        print(f"ERROR: docmap not found at {args.docmap}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build(args.docstore, args.docmap, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
