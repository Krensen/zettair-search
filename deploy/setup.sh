#!/usr/bin/env bash
# setup.sh — VPS provisioning for zettair-search (Top-1M enwiki corpus)
#
# Tested on: Ubuntu 24.04 ARM64 (Hetzner CCX13)
# Run as root or a user with sudo.
#
# Usage:
#   bash deploy/setup.sh
#
# What it does:
#   1.  Install system dependencies
#   2.  Clone both repos (if not present)
#   3.  Build Zettair binary (if not built)
#   4.  Download enwiki bz2 dump (~23 GB) to /mnt/wikipedia-source/
#   5.  Download clickstream files (15 months, ~7.4 GB) to wikipedia/
#   6.  Run select_top_articles.py → /mnt/wikipedia-source/top_titles.txt
#   7.  Run wiki2trec.py (bz2 streaming + --titles allowlist)
#         → /mnt/wikipedia-source/enwiki_top1m.trec + sidecar files
#   8.  Delete bz2 if volume free space < 25 GB
#   9.  Build docno map, click prior, autosuggest, docstore
#  10.  Build Zettair index in /mnt/wikipedia-source/wikiindex/
#  11.  Create service user, install systemd service
#  12.  Install cloudflared
#
# Every step is guarded by an existence check — re-run safely after any failure.
# The existing Simple English index at /opt/zettair/wikiindex/ is NOT touched.

set -euo pipefail

### ── Config ────────────────────────────────────────────────────────────────

INSTALL_DIR=/opt
ZETTAIR_SEARCH_REPO=https://github.com/Krensen/zettair-search.git
ZETTAIR_REPO=https://github.com/Krensen/zettair.git
SERVICE_USER=zettair

VOLUME=/mnt/wikipedia-source
WIKI_DIR="$INSTALL_DIR/zettair/wikipedia"
ZET_BIN="$INSTALL_DIR/zettair/devel/zet"

BZ2_FILE="$VOLUME/enwiki-latest-pages-articles.xml.bz2"
TITLES_FILE="$VOLUME/top_titles.txt"
TREC_FILE="$VOLUME/enwiki_top1m.trec"
INDEX_DIR="$VOLUME/wikiindex"

# Delete bz2 automatically if free space drops below this after TREC build
BZ2_DELETE_THRESHOLD_GB=25

log()  { echo "$(date '+%H:%M:%S') ── $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

### ── 1. System dependencies ────────────────────────────────────────────────

log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git gcc make autoconf automake libtool pkg-config \
    libz-dev curl wget

pip3 install --quiet --break-system-packages fastapi uvicorn

### ── 2. Clone repos ─────────────────────────────────────────────────────────

log "Cloning repos..."
cd "$INSTALL_DIR"
[ -d zettair-search ] || git clone "$ZETTAIR_SEARCH_REPO" zettair-search
[ -d zettair ]        || git clone "$ZETTAIR_REPO"        zettair

### ── 3. Build Zettair binary ───────────────────────────────────────────────

if [ ! -f "$ZET_BIN" ]; then
    log "Building Zettair..."
    cd "$INSTALL_DIR/zettair/devel"
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        BUILD_FLAG="--build=aarch64-unknown-linux-gnu"
    elif [ "$ARCH" = "x86_64" ]; then
        BUILD_FLAG=""
    else
        BUILD_FLAG=""
    fi
    ./configure $BUILD_FLAG
    make -j"$(nproc)"
    log "Binary built at: $ZET_BIN"
else
    log "Zettair binary already built — skipping."
fi

### ── 4. Verify volume mount ────────────────────────────────────────────────

[ -d "$VOLUME" ] || die "$VOLUME not found — mount the Hetzner volume first"
log "Volume $VOLUME present."

### ── 5. Download enwiki bz2 dump ───────────────────────────────────────────

BZ2_MIN_SIZE=$((20 * 1024 * 1024 * 1024))  # 20 GB in bytes
if [ -f "$TREC_FILE" ]; then
    log "TREC file already exists — skipping bz2 download."
elif [ -f "$BZ2_FILE" ] && [ "$(stat -c%s "$BZ2_FILE")" -gt "$BZ2_MIN_SIZE" ]; then
    log "enwiki bz2 dump already present — skipping download."
else
    log "Downloading enwiki bz2 dump (~23 GB, this takes ~30 min)..."
    wget -q --show-progress \
        "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles.xml.bz2" \
        -O "$BZ2_FILE"
    log "Download complete."
fi

### ── 6. Download clickstream files ─────────────────────────────────────────

log "Downloading Wikipedia clickstream data (15 months, ~7.4 GB total)..."
mkdir -p "$WIKI_DIR"

for MONTH in 2024-01 2024-02 2024-03 2024-04 2024-05 2024-06 \
             2024-07 2024-08 2024-09 2024-10 2024-11 2024-12 \
             2025-01 2025-02 2025-03; do
    FILE="$WIKI_DIR/clickstream-enwiki-${MONTH}.tsv.gz"
    if [ ! -f "$FILE" ]; then
        log "  Downloading clickstream $MONTH..."
        wget -q --show-progress \
            "https://dumps.wikimedia.org/other/clickstream/${MONTH}/clickstream-enwiki-${MONTH}.tsv.gz" \
            -O "$FILE" || { log "  WARNING: $MONTH not available, skipping"; rm -f "$FILE"; }
        sleep 2
    fi
done

### ── 7. select_top_articles.py → top_titles.txt ────────────────────────────

if [ ! -f "$TITLES_FILE" ]; then
    log "Running select_top_articles.py (scores 15 months of clickstream)..."
    python3 "$WIKI_DIR/select_top_articles.py" --top 1000000 --out "$TITLES_FILE"
    log "top_titles.txt written to $TITLES_FILE"
else
    log "top_titles.txt already exists — skipping select_top_articles.py."
fi

### ── 8. wiki2trec.py → enwiki_top1m.trec + sidecar files ──────────────────

if [ ! -f "$TREC_FILE" ]; then
    log "Running wiki2trec.py (bz2 streaming + title allowlist, ~4-8 hours)..."
    python3 "$WIKI_DIR/wiki2trec.py" "$BZ2_FILE" "$TREC_FILE" --titles "$TITLES_FILE"
    # wiki2trec writes sidecars alongside the TREC file (already on the volume)
    log "TREC file written to $TREC_FILE"
else
    log "TREC file already exists — skipping wiki2trec.py."
fi

### ── 9. Delete bz2 if disk is tight ────────────────────────────────────────

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

### ── 10. Pipeline: docno map, click prior, autosuggest, docstore ────────────
#
# These scripts use HERE-relative paths, so we run them from $WIKI_DIR.
# docno_map.tsv and autosuggest.json are small enough to live in $WIKI_DIR.
# click_prior.bin and enwiki_top1m.docstore/docmap are copied to $VOLUME.

cd "$WIKI_DIR"

log "Building docno map..."
if [ ! -f "$WIKI_DIR/docno_map.tsv" ]; then
    python3 build_docno_map.py "$TREC_FILE"
    # output lands at $WIKI_DIR/docno_map.tsv (hardcoded in script)
else
    log "  docno_map.tsv already exists — skipping."
fi

log "Extracting titles for autosuggest..."
if [ ! -f "$WIKI_DIR/enwiki_titles.txt" ]; then
    cut -f2 "$WIKI_DIR/docno_map.tsv" > "$WIKI_DIR/enwiki_titles.txt"
fi

log "Building click prior..."
if [ ! -f "$VOLUME/click_prior.bin" ]; then
    python3 build_click_prior.py
    # output lands at $WIKI_DIR/click_prior.bin
    cp "$WIKI_DIR/click_prior.bin" "$VOLUME/click_prior.bin"
    log "  click_prior.bin copied to $VOLUME"
else
    log "  click_prior.bin already exists — skipping."
fi

log "Building autosuggest index..."
if [ ! -f "$VOLUME/autosuggest.json" ]; then
    python3 build_autosuggest.py
    # output lands at $WIKI_DIR/autosuggest.json
    cp "$WIKI_DIR/autosuggest.json" "$VOLUME/autosuggest.json"
    log "  autosuggest.json copied to $VOLUME"
else
    log "  autosuggest.json already exists — skipping."
fi

log "Building docstore..."
if [ ! -f "$VOLUME/enwiki_top1m.docstore" ]; then
    python3 build_docstore.py "$TREC_FILE"
    # build_docstore derives output paths from TREC path — writes alongside TREC on volume
    log "  docstore written to $VOLUME"
else
    log "  enwiki_top1m.docstore already exists — skipping."
fi

### ── 11. Build Zettair index ───────────────────────────────────────────────

if [ ! -f "$INDEX_DIR/index.cfg" ]; then
    log "Building Zettair index (can take 30-60 min for 1M articles)..."
    mkdir -p "$INDEX_DIR"
    cd "$INDEX_DIR"
    "$ZET_BIN" -i -f index "$TREC_FILE"
    log "Index built at $INDEX_DIR"
else
    log "Index already exists at $INDEX_DIR — skipping."
fi

### ── 12. Create service user and install systemd service ───────────────────

log "Creating service user '$SERVICE_USER'..."
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
# zettair-search repo stays owned by deploy so git pull works without sudo
chown -R deploy:deploy "$INSTALL_DIR/zettair-search"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/zettair"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$VOLUME"

log "Installing systemd service..."
cp "$INSTALL_DIR/zettair-search/deploy/zettair-search.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable zettair-search

### ── 13. Install cloudflared ───────────────────────────────────────────────

log "Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
https://pkg.cloudflare.com/cloudflare $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/cloudflare.list
    apt-get update -qq && apt-get install -y -qq cloudflared
fi
if [ -f "$INSTALL_DIR/zettair-search/deploy/cloudflared.service" ]; then
    cp "$INSTALL_DIR/zettair-search/deploy/cloudflared.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable cloudflared
fi

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
log "       echo 'einstein' | $ZET_BIN -f $INDEX_DIR/index --summary=plain --output=json -n 3"
log "  3. Restart the service (zettair-search.service already points at volume paths):"
log "       sudo systemctl restart zettair-search"
log "       sudo systemctl status zettair-search"
log "       curl 'http://localhost:8765/search?q=einstein'"
log "  4. Keep old /opt/zettair/wikiindex/ for one week before deleting."
log "═══════════════════════════════════════════════════════"
