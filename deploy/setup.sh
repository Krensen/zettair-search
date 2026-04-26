#!/usr/bin/env bash
# setup.sh — VPS provisioning for zettair-search (Top-1M enwiki corpus)
#
# Tested on: Ubuntu 24.04 ARM64 (Hetzner CCX13)
# Run as root or a user with sudo.
#
# Usage:
#   sudo bash deploy/setup.sh
#
# What it does:
#   1.  Install system dependencies
#   2.  Create users (deploy, zettair)
#   3.  Clone both repos (if not present)
#   4.  Build Zettair binary (if not built)
#   5.  Download enwiki bz2 dump to volume
#   6.  Download clickstream files to wikipedia/
#   7.  Run select_top_articles.py → top_titles.txt
#   8.  Run wiki2trec.py (bz2 streaming + --titles allowlist)
#   9.  Delete bz2 if volume free space < threshold
#  10.  Build docno map, click prior, autosuggest, docstore
#  11.  Build Zettair index
#  12.  Set permissions and install systemd service
#
# Note: Caddy (reverse proxy + TLS) is installed separately and not managed here.
#
# Every step is guarded by an existence check — re-run safely after any failure.

set -euo pipefail

### ── Config — edit these if you need to change anything ────────────────────

DEPLOY_USER=deploy                    # user that runs git pull and deploys code
SERVICE_USER=zettair                  # user that runs the search service

INSTALL_DIR=/opt
ZETTAIR_SEARCH_REPO=https://github.com/Krensen/zettair-search.git
ZETTAIR_REPO=https://github.com/Krensen/zettair.git

VOLUME=/mnt/wikipedia-source          # Hetzner volume mount point
CORPUS_SIZE=1000000                   # number of top articles to index

ENWIKI_DUMP_URL="https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles.xml.bz2"
BZ2_DELETE_THRESHOLD_GB=25            # auto-delete bz2 after TREC if free space below this

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

### ── Helpers ────────────────────────────────────────────────────────────────

log() { echo "$(date '+%H:%M:%S') ── $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

### ── 1. System dependencies ─────────────────────────────────────────────────

log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git gcc make autoconf automake libtool pkg-config \
    libz-dev curl wget

pip3 install --quiet --break-system-packages fastapi uvicorn

### ── 2. Create users ────────────────────────────────────────────────────────

log "Creating users..."
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
    log "  Created $DEPLOY_USER"
fi
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    log "  Created $SERVICE_USER"
fi

### ── 3. Clone repos ─────────────────────────────────────────────────────────

log "Cloning repos..."
[ -d "$SEARCH_DIR" ] || git clone "$ZETTAIR_SEARCH_REPO" "$SEARCH_DIR"
[ -d "$ZETTAIR_DIR" ] || git clone "$ZETTAIR_REPO" "$ZETTAIR_DIR"

### ── 4. Build Zettair binary ────────────────────────────────────────────────

if [ ! -f "$ZET_BIN" ]; then
    log "Building Zettair..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        BUILD_FLAG="--build=aarch64-unknown-linux-gnu"
    else
        BUILD_FLAG=""
    fi
    cd "$ZETTAIR_DIR/devel"
    ./configure $BUILD_FLAG
    make -j"$(nproc)"
    log "Binary built at: $ZET_BIN"
else
    log "Zettair binary already built — skipping."
fi

### ── 5. Verify volume mount ─────────────────────────────────────────────────

[ -d "$VOLUME" ] || die "$VOLUME not found — mount the Hetzner volume first"
log "Volume $VOLUME present."

### ── 6. Download enwiki bz2 dump ────────────────────────────────────────────

BZ2_MIN_SIZE=$((20 * 1024 * 1024 * 1024))  # 20 GB in bytes
if [ -f "$TREC_FILE" ]; then
    log "TREC file already exists — skipping bz2 download."
elif [ -f "$BZ2_FILE" ] && [ "$(stat -c%s "$BZ2_FILE")" -gt "$BZ2_MIN_SIZE" ]; then
    log "enwiki bz2 dump already present — skipping download."
else
    log "Downloading enwiki bz2 dump (~23 GB, this takes ~30 min)..."
    wget -q --show-progress "$ENWIKI_DUMP_URL" -O "$BZ2_FILE"
    log "Download complete."
fi

### ── 7. Download clickstream files ─────────────────────────────────────────

log "Downloading Wikipedia clickstream data..."
mkdir -p "$WIKI_DIR"
for MONTH in $CLICKSTREAM_MONTHS; do
    FILE="$WIKI_DIR/clickstream-enwiki-${MONTH}.tsv.gz"
    if [ ! -f "$FILE" ]; then
        log "  Downloading clickstream $MONTH..."
        wget -q --show-progress \
            "https://dumps.wikimedia.org/other/clickstream/${MONTH}/clickstream-enwiki-${MONTH}.tsv.gz" \
            -O "$FILE" || { log "  WARNING: $MONTH not available, skipping"; rm -f "$FILE"; }
        sleep 2
    fi
done

### ── 8. select_top_articles.py → top_titles.txt ─────────────────────────────

if [ ! -f "$TITLES_FILE" ]; then
    log "Running select_top_articles.py (CORPUS_SIZE=$CORPUS_SIZE)..."
    python3 "$WIKI_DIR/select_top_articles.py" --top "$CORPUS_SIZE" --out "$TITLES_FILE"
    log "top_titles.txt written to $TITLES_FILE"
else
    log "top_titles.txt already exists — skipping select_top_articles.py."
fi

### ── 9. wiki2trec.py → enwiki_top1m.trec + sidecar files ───────────────────

if [ ! -f "$TREC_FILE" ]; then
    log "Running wiki2trec.py (bz2 streaming + title allowlist, ~4-8 hours)..."
    python3 "$WIKI_DIR/wiki2trec.py" "$BZ2_FILE" "$TREC_FILE" --titles "$TITLES_FILE"
    log "TREC file written to $TREC_FILE"
else
    log "TREC file already exists — skipping wiki2trec.py."
fi

### ── 10. Delete bz2 if disk is tight ────────────────────────────────────────

if [ -f "$BZ2_FILE" ]; then
    FREE_GB=$(df -BG "$VOLUME" | awk 'NR==2 {gsub("G",""); print $4}')
    if [ "$FREE_GB" -lt "$BZ2_DELETE_THRESHOLD_GB" ]; then
        log "Free space is ${FREE_GB}GB < ${BZ2_DELETE_THRESHOLD_GB}GB — deleting bz2..."
        rm -f "$BZ2_FILE"
        log "bz2 deleted. Free space: $(df -BG "$VOLUME" | awk 'NR==2 {print $4}')"
    else
        log "Free space is ${FREE_GB}GB — keeping bz2."
    fi
fi

### ── 11. Pipeline: docno map, click prior, autosuggest, docstore ────────────

log "Building docno map..."
if [ ! -f "$WIKI_DIR/docno_map.tsv" ]; then
    python3 "$WIKI_DIR/build_docno_map.py" "$TREC_FILE"
else
    log "  docno_map.tsv already exists — skipping."
fi

log "Extracting titles for autosuggest..."
if [ ! -f "$WIKI_DIR/enwiki_titles.txt" ]; then
    cut -f2 "$WIKI_DIR/docno_map.tsv" > "$WIKI_DIR/enwiki_titles.txt"
fi

log "Building click prior..."
if [ ! -f "$VOLUME/click_prior.bin" ]; then
    # build_click_prior.py uses HERE-relative paths for clickstream files and output
    (cd "$WIKI_DIR" && python3 build_click_prior.py)
    cp "$WIKI_DIR/click_prior.bin" "$VOLUME/click_prior.bin"
    log "  click_prior.bin copied to $VOLUME"
else
    log "  click_prior.bin already exists — skipping."
fi

log "Building autosuggest index..."
if [ ! -f "$VOLUME/autosuggest.json" ]; then
    # build_autosuggest.py uses HERE-relative paths for clickstream and titles files
    (cd "$WIKI_DIR" && python3 build_autosuggest.py)
    cp "$WIKI_DIR/autosuggest.json" "$VOLUME/autosuggest.json"
    log "  autosuggest.json copied to $VOLUME"
else
    log "  autosuggest.json already exists — skipping."
fi

log "Building docstore..."
if [ ! -f "$VOLUME/enwiki_top1m.docstore" ]; then
    python3 "$WIKI_DIR/build_docstore.py" "$TREC_FILE"
    log "  docstore written alongside TREC on $VOLUME"
else
    log "  enwiki_top1m.docstore already exists — skipping."
fi

log "Building dbkey map..."
DBKEYS_FILE="$VOLUME/enwiki_top1m.dbkeys.tsv"
if [ ! -f "$DBKEYS_FILE" ]; then
    # If wiki2trec.py wrote the dbkeys file natively, it's already here. Otherwise
    # generate it from top_titles.txt + docno_map.tsv (one-shot bootstrap).
    python3 "$WIKI_DIR/build_dbkey_map.py" "$TITLES_FILE" "$WIKI_DIR/docno_map.tsv" "$DBKEYS_FILE"
else
    log "  enwiki_top1m.dbkeys.tsv already exists — skipping."
fi

### ── 12. Build Zettair index ────────────────────────────────────────────────

if [ ! -f "$INDEX_DIR/index.cfg" ]; then
    log "Building Zettair index (can take 30-60 min for 1M articles)..."
    mkdir -p "$INDEX_DIR"
    cd "$INDEX_DIR"
    "$ZET_BIN" -i -f index "$TREC_FILE"
    log "Index built at $INDEX_DIR"
else
    log "Index already exists at $INDEX_DIR — skipping."
fi

### ── 13. Set permissions and install systemd service ────────────────────────

log "Setting permissions..."

# deploy owns both repos and can git pull either without sudo.
# World-readable (+rX) so the zettair service user can read server.py and the zet binary.
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SEARCH_DIR" "$ZETTAIR_DIR"
chmod -R o+rX "$SEARCH_DIR"
chmod -R o+rX "$ZETTAIR_DIR"

# zettair owns the volume — all runtime data lives here.
# deploy does not need access after setup is complete.
chown -R "$SERVICE_USER:$SERVICE_USER" "$VOLUME"
chmod 750 "$VOLUME"

log "Installing systemd service..."
cp "$SEARCH_DIR/deploy/zettair-search.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable zettair-search

### ── Done ───────────────────────────────────────────────────────────────────

log ""
log "═══════════════════════════════════════════════════════"
log "  Setup complete!"
log ""
log "  Cutover checklist:"
log "  1. Verify artifacts:"
log "       ls -lh $INDEX_DIR/index.cfg"
log "       ls -lh $VOLUME/enwiki_top1m.docstore"
log "       ls -lh $VOLUME/enwiki_top1m_snippets.store"
log "  2. Smoke-test the index:"
log "       sudo -u $SERVICE_USER $ZET_BIN -f $INDEX_DIR/index --summary=plain --output=json -n 3 <<< 'einstein'"
log "  3. Restart the service:"
log "       sudo systemctl restart zettair-search"
log "       sudo systemctl status zettair-search"
log "       curl 'http://localhost:8765/search?q=einstein'"
log "  4. Keep old /opt/zettair/wikiindex/ for one week before deleting."
log "═══════════════════════════════════════════════════════"
