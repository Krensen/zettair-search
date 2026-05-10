#!/usr/bin/env bash
# setup.sh — VPS provisioning for zettair-search (Top-N enwiki corpus)
#
# Tested on: Ubuntu 24.04 ARM64 (Hetzner CCX13)
# Run as root or a user with sudo:
#
#   sudo bash deploy/setup.sh
#
# This is the ONLY command anyone should ever run on the box. It is fully
# idempotent: re-running it after any change (new corpus, new clickstream,
# new C source) detects what is stale and rebuilds only that. Never run
# the python pipeline scripts manually.
#
# Permission model:
#   - deploy owns both repos (/opt/zettair-search, /opt/zettair). git pull,
#     pipeline scripts, and the zet build all run as deploy.
#   - zettair owns the volume (/mnt/wikipedia-source/) and runs the search
#     service. wget into the volume, wiki2trec, build_docstore, zet -i, and
#     all writes into the volume run as zettair.
#   - root only does what genuinely needs root: apt, useradd, /etc/systemd
#     writes, systemctl, the initial chown to set ownership on volume + repos.
#
# Staleness model: each derived artefact is rebuilt when any of its inputs
# is newer (mtime check via `is_stale`). No "skip if exists" — that has
# bitten us before with click_prior.bin going out of sync with the index.

set -euo pipefail

### ── Config — edit these if you need to change anything ────────────────────
# All can be overridden by environment variable for tests/test_setup.sh.

DEPLOY_USER="${DEPLOY_USER:-deploy}"
SERVICE_USER="${SERVICE_USER:-zettair}"

INSTALL_DIR="${INSTALL_DIR:-/opt}"
ZETTAIR_SEARCH_REPO="${ZETTAIR_SEARCH_REPO:-https://github.com/Krensen/zettair-search.git}"
ZETTAIR_REPO="${ZETTAIR_REPO:-https://github.com/Krensen/zettair.git}"

VOLUME="${VOLUME:-/mnt/wikipedia-source}"
CORPUS_SIZE="${CORPUS_SIZE:-1500000}"

ENWIKI_DUMP_URL="${ENWIKI_DUMP_URL:-https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles.xml.bz2}"
BZ2_DELETE_THRESHOLD_GB="${BZ2_DELETE_THRESHOLD_GB:-30}"

CLICKSTREAM_MONTHS="
    2024-01 2024-02 2024-03 2024-04 2024-05 2024-06
    2024-07 2024-08 2024-09 2024-10 2024-11 2024-12
    2025-01 2025-02 2025-03
"

### ── Derived paths — no need to change these ───────────────────────────────

SEARCH_DIR="$INSTALL_DIR/zettair-search"
ZETTAIR_DIR="$INSTALL_DIR/zettair"
WIKI_DIR="$ZETTAIR_DIR/wikipedia"
ZET_BIN="$ZETTAIR_DIR/devel/zet"

BZ2_FILE="$VOLUME/enwiki-latest-pages-articles.xml.bz2"
TITLES_FILE="$VOLUME/top_titles.txt"
TREC_FILE="$VOLUME/enwiki_top1m.trec"
INDEX_DIR="$VOLUME/wikiindex"
INDEX_PREFIX="$INDEX_DIR/index"

# Indexer-emitted sidecars. Aligned with the index by construction —
# zet writes them in the same loop that assigns docids.
INDEX_PARAM="${INDEX_PREFIX}.param.0"
INDEX_FIELD_LENGTHS="${INDEX_PREFIX}.field_lengths"
INDEX_FIELD_STATS="${INDEX_PREFIX}.field_stats"
INDEX_DOCNO_MAP="${INDEX_PREFIX}.docno_map.tsv"
INDEX_CLICK_PRIOR="${INDEX_PREFIX}.click_prior.bin"

DOCSTORE="$VOLUME/enwiki_top1m.docstore"
DOCMAP="$VOLUME/enwiki_top1m.docmap"
AUTOSUGGEST="$VOLUME/autosuggest.json"
URLS_STORE="$VOLUME/enwiki_top1m_urls.store"
URLS_MAP="$VOLUME/enwiki_top1m_urls.map"

### ── Helpers ────────────────────────────────────────────────────────────────

log() { echo "$(date '+%H:%M:%S') ── $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# DRY_RUN=1 makes setup.sh log decisions and skip side effects, for
# tests/test_setup.sh and for safe debugging on prod. The staleness
# checks (is_stale / is_stale_glob) still run unchanged so the
# decisions reflect what a real run would do.
DRY_RUN="${DRY_RUN:-0}"
decided() {
    # decided ARTEFACT REASON
    # Logs a stable line that the test harness can grep for.
    echo "DECISION: rebuild $1 (reason: $2)"
}
skipped() {
    echo "DECISION: skip $1 (reason: $2)"
}
dry() {
    # dry CMD ARGS...
    # Run CMD in real mode, log it in dry-run mode.
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would run: $*"
    else
        "$@"
    fi
}

# Run a command as the deploy user. Repo writes use this.
as_deploy() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would run as $DEPLOY_USER: $*"
    else
        sudo -u "$DEPLOY_USER" "$@"
    fi
}

# Run a command as the zettair (service) user. Volume writes use this.
as_zettair() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would run as $SERVICE_USER: $*"
    else
        sudo -u "$SERVICE_USER" "$@"
    fi
}

# is_stale OUTPUT INPUT [INPUT...]
# Returns 0 (stale, needs rebuild) if OUTPUT is missing OR any INPUT is
# newer than OUTPUT. Returns 1 (fresh) otherwise.
# Inputs that don't exist are silently skipped (so optional inputs work).
is_stale() {
    local out="$1"; shift
    [ -e "$out" ] || return 0
    local in
    for in in "$@"; do
        [ -e "$in" ] || continue
        if [ "$in" -nt "$out" ]; then
            return 0
        fi
    done
    return 1
}

# is_stale_dir OUTPUT INPUT_GLOB
# Like is_stale, but INPUT_GLOB is shell-expanded. Useful for "any
# clickstream file is newer than click_prior".
is_stale_glob() {
    local out="$1"; shift
    [ -e "$out" ] || return 0
    local in
    for in in "$@"; do
        # ignore literal globs that didn't match anything
        [ -e "$in" ] || continue
        if [ "$in" -nt "$out" ]; then
            return 0
        fi
    done
    return 1
}

### ── 1. System dependencies (root) — skipped on subsequent runs ────────────
#
# A marker file says "this box is provisioned" so re-runs (e.g. from
# deploy.sh) skip the slow apt-get/useradd path. Bumps to packages or
# user setup require deleting /etc/zettair-setup-done before re-running.

SETUP_MARKER="${SETUP_MARKER_OVERRIDE:-/etc/zettair-setup-done}"
if [ ! -f "$SETUP_MARKER" ]; then
    decided system-packages "marker $SETUP_MARKER missing (first run)"
    log "Installing system packages (first run)..."
    dry apt-get update -qq
    dry apt-get install -y -qq \
        python3 python3-pip python3-venv \
        git gcc make autoconf automake libtool pkg-config \
        libz-dev curl wget

    dry pip3 install --quiet --break-system-packages fastapi uvicorn

    log "Creating users..."
    if ! id "$DEPLOY_USER" &>/dev/null; then
        dry useradd --create-home --shell /bin/bash "$DEPLOY_USER"
        log "  Created $DEPLOY_USER"
    fi
    if ! id "$SERVICE_USER" &>/dev/null; then
        dry useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        log "  Created $SERVICE_USER"
    fi

    dry touch "$SETUP_MARKER"
else
    skipped system-packages "marker $SETUP_MARKER present"
    log "System provisioned ($SETUP_MARKER exists) — skipping apt/useradd."
fi

### ── 3. Verify volume mount and set ownership upfront (root) ────────────────

[ -d "$VOLUME" ] || die "$VOLUME not found — mount the Hetzner volume first"
log "Volume $VOLUME present."

# Set volume ownership ONCE, before anything writes to it. Subsequent writes
# are all done via `sudo -u zettair`, which creates files as zettair naturally.
dry chown "$SERVICE_USER:$SERVICE_USER" "$VOLUME"
dry chmod 750 "$VOLUME"

### ── 4. Clone/update repos (deploy) ─────────────────────────────────────────

log "Cloning/updating repos..."
dry mkdir -p "$INSTALL_DIR"
dry chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR" 2>/dev/null || true

if [ -d "$SEARCH_DIR" ]; then
    skipped clone-zettair-search "already present"
    as_deploy bash -c "cd '$SEARCH_DIR' && git pull origin main"
else
    decided clone-zettair-search "directory missing"
    as_deploy git clone "$ZETTAIR_SEARCH_REPO" "$SEARCH_DIR"
fi

# requirements.txt may have new entries between deploys; pip is idempotent
# so this is a fast no-op when nothing changed.
if [ -f "$SEARCH_DIR/requirements.txt" ]; then
    dry pip3 install --quiet --break-system-packages -r "$SEARCH_DIR/requirements.txt"
fi

if [ -d "$ZETTAIR_DIR" ]; then
    skipped clone-zettair "already present"
    as_deploy bash -c "cd '$ZETTAIR_DIR' && git pull origin main"
else
    decided clone-zettair "directory missing"
    as_deploy git clone "$ZETTAIR_REPO" "$ZETTAIR_DIR"
fi

# World-readable so the zettair service user can read server.py and the zet binary
dry chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SEARCH_DIR" "$ZETTAIR_DIR"
dry chmod -R o+rX "$SEARCH_DIR" "$ZETTAIR_DIR"

# Logs dir for the search service must be writable by the service user.
dry mkdir -p "$SEARCH_DIR/logs"
dry chown -R "$SERVICE_USER:$SERVICE_USER" "$SEARCH_DIR/logs"

### ── 5. Build Zettair binary (deploy) ──────────────────────────────────────
#
# After `make`, libtool leaves the binary as a wrapper script at devel/zet
# that re-links the real ELF binary on first invocation if it can't find a
# writable build tree. That breaks when a non-deploy user (zettair) runs it.
# We replace the wrapper with the real binary from .libs/ and make the
# shared library globally findable so zet runs regardless of CWD or invoker.

BUILD_REASON=""
if [ ! -f "$ZET_BIN" ]; then
    BUILD_REASON="zet binary missing"
elif ! file "$ZET_BIN" 2>/dev/null | grep -q ELF; then
    BUILD_REASON="zet binary is wrapper script (not ELF)"
else
    # Source-newer-than-binary check. Catches: someone edited a C file,
    # `git pull` brought in new source, etc. mtime-based — survives
    # across runs without needing a saved HEAD marker.
    NEWER_SRC=""
    if [ -d "$ZETTAIR_DIR/devel/src" ]; then
        NEWER_SRC=$(find "$ZETTAIR_DIR/devel/src" \
            \( -name '*.c' -o -name '*.h' \) -newer "$ZET_BIN" 2>/dev/null \
            | head -1 || true)
    fi
    if [ -n "$NEWER_SRC" ]; then
        BUILD_REASON="C source newer than zet binary: $NEWER_SRC"
    fi
fi

if [ -n "$BUILD_REASON" ]; then
    decided zet-binary "$BUILD_REASON"
    log "Building Zettair..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        BUILD_FLAG="--build=aarch64-unknown-linux-gnu"
    else
        BUILD_FLAG=""
    fi
    if [ ! -f "$ZETTAIR_DIR/devel/Makefile" ]; then
        as_deploy bash -c "cd '$ZETTAIR_DIR/devel' && ./configure $BUILD_FLAG"
    fi
    as_deploy bash -c "cd '$ZETTAIR_DIR/devel' && make -j$(nproc)"

    # Replace the libtool wrapper with the real ELF binary.
    if [ -f "$ZETTAIR_DIR/devel/.libs/zet" ]; then
        dry cp "$ZETTAIR_DIR/devel/.libs/zet" "$ZET_BIN"
        dry chmod +x "$ZET_BIN"
        dry chown "$DEPLOY_USER:$DEPLOY_USER" "$ZET_BIN"
    fi

    # Register libzet.so so the bare binary can find it without LD_LIBRARY_PATH.
    if [ -f "$ZETTAIR_DIR/devel/.libs/libzet.so.0" ]; then
        dry ln -sf "$ZETTAIR_DIR/devel/.libs/libzet.so.0" /usr/local/lib/libzet.so.0
        dry ldconfig
    fi

    log "Binary installed at: $ZET_BIN"
    # If zet rebuilt, the index might be stale wrt new on-disk format,
    # but for now we trust the format hasn't changed unless the param
    # file is missing. Force a reindex by deleting the index dir if you
    # actually need a format-change rebuild.
else
    skipped zet-binary "binary present, ELF, no source newer"
    log "Zettair binary up to date — skipping."
fi

### ── 6. Download enwiki bz2 dump (zettair, to volume) ──────────────────────

BZ2_MIN_SIZE=$((20 * 1024 * 1024 * 1024))  # 20 GB in bytes
if [ -f "$TREC_FILE" ]; then
    skipped bz2-download "TREC file already exists"
    log "TREC file already exists — skipping bz2 download."
elif [ -f "$BZ2_FILE" ] && [ "$(stat -c%s "$BZ2_FILE" 2>/dev/null || stat -f%z "$BZ2_FILE" 2>/dev/null)" -gt "$BZ2_MIN_SIZE" ]; then
    skipped bz2-download "bz2 already present and >20GB"
    log "enwiki bz2 dump already present — skipping download."
else
    decided bz2-download "no TREC and no bz2"
    log "Downloading enwiki bz2 dump (~23 GB, this takes ~30 min)..."
    as_zettair wget -q --show-progress "$ENWIKI_DUMP_URL" -O "$BZ2_FILE"
    log "Download complete."
fi

### ── 7. Download clickstream files (deploy, to wiki dir in deploy repo) ────

log "Downloading Wikipedia clickstream data..."
as_deploy mkdir -p "$WIKI_DIR"
for MONTH in $CLICKSTREAM_MONTHS; do
    FILE="$WIKI_DIR/clickstream-enwiki-${MONTH}.tsv.gz"
    if [ ! -f "$FILE" ]; then
        decided "clickstream-$MONTH" "missing"
        log "  Downloading clickstream $MONTH..."
        as_deploy wget -q --show-progress \
            "https://dumps.wikimedia.org/other/clickstream/${MONTH}/clickstream-enwiki-${MONTH}.tsv.gz" \
            -O "$FILE" || { log "  WARNING: $MONTH not available, skipping"; rm -f "$FILE"; }
        sleep 2
    fi
done

### ── 8. select_top_articles.py → top_titles.txt (zettair, to volume) ────────

if [ ! -f "$TITLES_FILE" ]; then
    decided top-titles "missing"
    log "Running select_top_articles.py (CORPUS_SIZE=$CORPUS_SIZE)..."
    as_zettair python3 "$WIKI_DIR/select_top_articles.py" \
        --top "$CORPUS_SIZE" --out "$TITLES_FILE"
    log "top_titles.txt written to $TITLES_FILE"
else
    skipped top-titles "already exists"
    log "top_titles.txt already exists — skipping select_top_articles.py."
fi

### ── 9. wiki2trec.py → TREC + sidecar files (zettair, to volume) ───────────

if [ ! -f "$TREC_FILE" ]; then
    decided trec "missing"
    log "Running wiki2trec.py (bz2 streaming + title allowlist, ~4-8 hours)..."
    as_zettair python3 "$WIKI_DIR/wiki2trec.py" \
        "$BZ2_FILE" "$TREC_FILE" --titles "$TITLES_FILE"
    log "TREC file written to $TREC_FILE"
else
    skipped trec "already exists"
    log "TREC file already exists — skipping wiki2trec.py."
fi

### ── 10. Delete bz2 if disk is tight (zettair) ─────────────────────────────

if [ -f "$BZ2_FILE" ]; then
    FREE_GB=$(df -BG "$VOLUME" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo 999)
    if [ "${FREE_GB:-999}" -lt "$BZ2_DELETE_THRESHOLD_GB" ] 2>/dev/null; then
        log "Free space is ${FREE_GB}GB < ${BZ2_DELETE_THRESHOLD_GB}GB — deleting bz2..."
        as_zettair rm -f "$BZ2_FILE"
        log "bz2 deleted."
    else
        log "Free space is ${FREE_GB}GB — keeping bz2."
    fi
fi

### ── 11. Build Zettair index (zettair, to volume) ──────────────────────────
#
# zet -i emits the index AND three sidecars in one pass, all aligned with
# the docid space because they're written in the same loop:
#   index.field_lengths   — per-doc per-field word counts (PRD-019)
#   index.field_stats     — per-field corpus averages (PRD-019)
#   index.docno_map.tsv   — docid -> docno mapping (Phase 2)
# Stale if TREC is newer than the index param file or any sidecar is missing.

INDEX_REASON=""
if [ ! -f "$INDEX_PARAM" ]; then
    INDEX_REASON="index.param.0 missing"
elif [ -e "$TREC_FILE" ] && [ "$TREC_FILE" -nt "$INDEX_PARAM" ]; then
    INDEX_REASON="TREC newer than index.param.0"
elif [ ! -f "$INDEX_FIELD_LENGTHS" ]; then
    INDEX_REASON="field_lengths sidecar missing"
elif [ ! -f "$INDEX_FIELD_STATS" ]; then
    INDEX_REASON="field_stats sidecar missing"
elif [ ! -f "$INDEX_DOCNO_MAP" ]; then
    INDEX_REASON="docno_map sidecar missing"
fi
if [ -n "$INDEX_REASON" ]; then
    decided index "$INDEX_REASON"
    log "Building Zettair index (~10 min for 1.5M articles)..."
    as_zettair mkdir -p "$INDEX_DIR"
    # Wipe any partial index from a previous failed run; otherwise zet -i
    # may refuse to create a fresh index over an existing one.
    as_zettair bash -c "rm -f '$INDEX_DIR'/index.* '$INDEX_DIR'/*.tsv"
    as_zettair "$ZET_BIN" -i -f "$INDEX_PREFIX" "$TREC_FILE"
    log "Index built at $INDEX_DIR with sidecars."
else
    skipped index "param + 3 sidecars present and up to date"
    log "Index up to date — skipping."
fi

### ── 12. Build click_prior.bin (zettair, to index dir) ─────────────────────
#
# Aligned with the live index docid space via index.docno_map.tsv emitted
# by zet itself. Stale if any clickstream file or the docno_map is newer
# than click_prior.bin.

CLICK_PRIOR_REASON=""
if [ ! -f "$INDEX_CLICK_PRIOR" ]; then
    CLICK_PRIOR_REASON="click_prior.bin missing"
elif [ -e "$INDEX_DOCNO_MAP" ] && [ "$INDEX_DOCNO_MAP" -nt "$INDEX_CLICK_PRIOR" ]; then
    CLICK_PRIOR_REASON="docno_map newer than click_prior.bin"
elif is_stale_glob "$INDEX_CLICK_PRIOR" "$WIKI_DIR"/clickstream-enwiki-*.tsv.gz; then
    CLICK_PRIOR_REASON="a clickstream file is newer than click_prior.bin"
fi
if [ -n "$CLICK_PRIOR_REASON" ]; then
    decided click-prior "$CLICK_PRIOR_REASON"
    log "Building click prior from $INDEX_DOCNO_MAP..."
    # Need clickstream files alongside the script (build_click_prior.py
    # globs its own dir). Logs land in the wiki-dir-relative logs/ dir
    # (env-overridable) — make it writable by zettair.
    as_zettair mkdir -p "$WIKI_DIR/logs"
    as_zettair bash -c "cd '$WIKI_DIR' && python3 build_click_prior.py --index '$INDEX_PREFIX'"
    log "click_prior.bin written to $INDEX_CLICK_PRIOR"
else
    skipped click-prior "click_prior.bin present and newer than its inputs"
    log "click_prior.bin up to date — skipping."
fi

### ── 13. Build autosuggest (zettair, to volume) ────────────────────────────
#
# Stale if any clickstream file is newer than autosuggest.json.

AUTOSUGGEST_REASON=""
if [ ! -f "$AUTOSUGGEST" ]; then
    AUTOSUGGEST_REASON="autosuggest.json missing"
elif is_stale_glob "$AUTOSUGGEST" "$WIKI_DIR"/clickstream-enwiki-*.tsv.gz; then
    AUTOSUGGEST_REASON="a clickstream file is newer than autosuggest.json"
fi
if [ -n "$AUTOSUGGEST_REASON" ]; then
    decided autosuggest "$AUTOSUGGEST_REASON"
    log "Building autosuggest index..."
    as_zettair bash -c "cd '$WIKI_DIR' && python3 build_autosuggest.py"
    as_zettair cp "$WIKI_DIR/autosuggest.json" "$AUTOSUGGEST"
    log "autosuggest.json copied to $VOLUME"
else
    skipped autosuggest "autosuggest.json present and newer than its inputs"
    log "autosuggest.json up to date — skipping."
fi

### ── 14. Build docstore (zettair, to volume) ────────────────────────────────
#
# Stale if TREC is newer than the docstore.

DOCSTORE_REASON=""
if [ ! -f "$DOCSTORE" ]; then
    DOCSTORE_REASON="docstore missing"
elif [ -e "$TREC_FILE" ] && [ "$TREC_FILE" -nt "$DOCSTORE" ]; then
    DOCSTORE_REASON="TREC newer than docstore"
fi
if [ -n "$DOCSTORE_REASON" ]; then
    decided docstore "$DOCSTORE_REASON"
    log "Building docstore..."
    as_zettair python3 "$WIKI_DIR/build_docstore.py" "$TREC_FILE"
    log "docstore written to $DOCSTORE"
else
    skipped docstore "docstore present and newer than TREC"
    log "docstore up to date — skipping."
fi

### ── 15. Build URLs store if missing (zettair, to volume) ──────────────────
# wiki2trec.py writes the URL store natively for fresh builds (PRD-015).
# This step is a fallback for indexes that pre-date that — bootstrap from
# a dbkeys.tsv file if one exists.

if [ ! -f "$URLS_STORE" ]; then
    DBKEYS_FILE="$VOLUME/enwiki_top1m.dbkeys.tsv"
    if [ -f "$DBKEYS_FILE" ]; then
        decided urls-store "missing, dbkeys.tsv present"
        log "Bootstrapping URLs store from dbkeys.tsv..."
        as_zettair python3 "$WIKI_DIR/build_urls_store.py" \
            "$DBKEYS_FILE" "$URLS_STORE" "$URLS_MAP"
    else
        log "WARNING: no urls store and no dbkeys.tsv to bootstrap from — punctuation links will 404"
    fi
else
    skipped urls-store "already present"
    log "URLs store present."
fi

### ── 16. Install systemd service (root) — always rsync the unit file ───────

decided systemd-unit "always rsync"
log "Installing systemd service..."
dry cp "$SEARCH_DIR/deploy/zettair-search.service" /etc/systemd/system/
dry systemctl daemon-reload
dry systemctl enable zettair-search

### ── 17. Verify ownership (loud failure if anything is misowned) ───────────

log "Verifying ownership..."
ROOT_FILES_VOLUME=$(find "$VOLUME" -mindepth 1 -user root -not -path '*/lost+found*' 2>/dev/null | head -5)
ROOT_FILES_REPOS=$(find "$SEARCH_DIR" "$ZETTAIR_DIR" -user root 2>/dev/null | head -5)

if [ -n "$ROOT_FILES_VOLUME" ] || [ -n "$ROOT_FILES_REPOS" ]; then
    log "  WARNING: root-owned files found (showing up to 5 per area):"
    [ -n "$ROOT_FILES_VOLUME" ] && echo "    on volume:" && echo "$ROOT_FILES_VOLUME" | sed 's/^/      /'
    [ -n "$ROOT_FILES_REPOS"  ] && echo "    in repos:"  && echo "$ROOT_FILES_REPOS"  | sed 's/^/      /'
    log "  Fix:  sudo chown -R $SERVICE_USER:$SERVICE_USER $VOLUME"
    log "  Fix:  sudo chown -R $DEPLOY_USER:$DEPLOY_USER $SEARCH_DIR $ZETTAIR_DIR"
else
    log "  All ownership correct — volume:$SERVICE_USER, repos:$DEPLOY_USER, logs/:$SERVICE_USER."
fi

### ── 18. Restart service ───────────────────────────────────────────────────

decided service-restart "always restart after config refresh"
log "Restarting zettair-search..."
dry systemctl restart zettair-search
if [ "$DRY_RUN" != "1" ]; then
    sleep 3
    if curl -sf --max-time 5 "http://localhost:8765/search?q=test&n=1" > /dev/null; then
        log "  Service is up and responding."
    else
        log "  WARNING: health check failed — see: journalctl -u zettair-search -n 50"
    fi
fi

### ── Done ───────────────────────────────────────────────────────────────────

log ""
log "═══════════════════════════════════════════════════════"
log "  Setup complete."
log ""
log "  Re-run this script any time:"
log "    - C source changed                  -> rebuilds zet, may force reindex"
log "    - new clickstream file dropped in   -> rebuilds click prior + autosuggest"
log "    - corpus refreshed (new TREC)       -> rebuilds index + all sidecars"
log "    - nothing changed                   -> skips everything, restarts service"
log "  Each artefact is rebuilt only when its inputs are newer."
log "═══════════════════════════════════════════════════════"
