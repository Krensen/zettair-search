#!/usr/bin/env bash
# setup.sh — one-time VPS provisioning for zettair-search
#
# Tested on: Ubuntu 24.04 ARM64 (Hetzner CAX21)
# Run as root or a user with sudo.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Krensen/zettair-search/main/deploy/setup.sh | bash
#   — or —
#   bash deploy/setup.sh
#
# What it does:
#   1. Install system dependencies
#   2. Clone both repos
#   3. Build Zettair binary
#   4. Download Simple English Wikipedia dump
#   5. Convert dump → TREC + sidecar files
#   6. Build Zettair index
#   7. Download clickstream files (15 months)
#   8. Build click prior, autosuggest, docstore
#   9. Install systemd services
#  10. Install cloudflared
#
# After running this script:
#   - Edit /etc/systemd/system/zettair-search.service to set your domain/paths
#   - Copy your Cloudflare tunnel credentials to /etc/cloudflared/
#   - systemctl start zettair-search cloudflared

set -euo pipefail

### ── Config ────────────────────────────────────────────────────────────────

INSTALL_DIR=/opt
ZETTAIR_SEARCH_REPO=https://github.com/Krensen/zettair-search.git
ZETTAIR_REPO=https://github.com/Krensen/zettair.git
WIKI_DUMP_URL=https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2
SERVICE_USER=zettair

log() { echo "$(date '+%H:%M:%S') ── $*"; }

### ── 1. System dependencies ────────────────────────────────────────────────

log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git gcc make autoconf automake libtool pkg-config \
    libz-dev curl wget bzip2

pip3 install --quiet --break-system-packages fastapi uvicorn

### ── 2. Clone repos ─────────────────────────────────────────────────────────

log "Cloning repos..."
cd "$INSTALL_DIR"
[ -d zettair-search ] || git clone "$ZETTAIR_SEARCH_REPO" zettair-search
[ -d zettair ]        || git clone "$ZETTAIR_REPO"        zettair

### ── 3. Build Zettair binary ───────────────────────────────────────────────

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
log "Binary at: $INSTALL_DIR/zettair/devel/zet"

### ── 4. Download Wikipedia dump ────────────────────────────────────────────

log "Downloading Simple English Wikipedia dump (~330MB)..."
mkdir -p "$INSTALL_DIR/zettair/wikipedia"
cd "$INSTALL_DIR/zettair/wikipedia"

if [ ! -f simplewiki.xml ]; then
    wget -q --show-progress -O simplewiki-latest.xml.bz2 "$WIKI_DUMP_URL"
    log "Decompressing dump (~1.5GB)..."
    bunzip2 simplewiki-latest.xml.bz2
    mv simplewiki-latest.xml simplewiki.xml
fi

### ── 5. Convert to TREC + extract sidecars ──────────────────────────────────

log "Converting XML → TREC (this takes ~5 min)..."
if [ ! -f simplewiki.trec ]; then
    python3 wiki2trec.py simplewiki.xml simplewiki.trec
fi
# Produces: simplewiki.trec, simplewiki_snippets.json, simplewiki_images.json

### ── 6. Build Zettair index ────────────────────────────────────────────────

log "Building search index (this takes ~5–20 min)..."
mkdir -p "$INSTALL_DIR/zettair/wikiindex"
if [ ! -f "$INSTALL_DIR/zettair/wikiindex/index.cfg" ]; then
    cd "$INSTALL_DIR/zettair/wikiindex"
    ../devel/zet -i -f index ../wikipedia/simplewiki.trec
fi

### ── 7. Download clickstream files ─────────────────────────────────────────

log "Downloading Wikipedia clickstream data (15 months, ~6.5GB total)..."
cd "$INSTALL_DIR/zettair/wikipedia"

for MONTH in 2024-01 2024-02 2024-03 2024-04 2024-05 2024-06 \
             2024-07 2024-08 2024-09 2024-10 2024-11 2024-12 \
             2025-01 2025-02 2025-03; do
    FILE="clickstream-enwiki-${MONTH}.tsv.gz"
    if [ ! -f "$FILE" ]; then
        log "  Downloading $MONTH..."
        wget -q --show-progress \
            "https://dumps.wikimedia.org/other/clickstream/${MONTH}/${FILE}" \
            -O "$FILE" || { log "  WARNING: $MONTH not available yet, skipping"; rm -f "$FILE"; }
        sleep 2  # be polite to Wikimedia
    fi
done

### ── 8. Build pipeline: docno map, click prior, autosuggest, docstore ───────

log "Extracting article titles..."
python3 -c "
import json
with open('simplewiki_snippets.json') as f:
    titles = list(json.load(f).keys())
with open('simplewiki_titles.txt', 'w') as f:
    f.write('\n'.join(titles))
print(f'{len(titles):,} titles written')
"

log "Building docno map..."
python3 build_docno_map.py

log "Extracting titles list for autosuggest..."
cut -f2 docno_map.tsv > simplewiki_titles.txt

log "Building click prior..."
python3 build_click_prior.py

log "Building autosuggest index (~10 min)..."
python3 build_autosuggest.py

log "Building docstore for query-biased summaries (~30 sec)..."
python3 build_docstore.py

### ── 9. Create service user ────────────────────────────────────────────────

log "Creating service user '$SERVICE_USER'..."
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/zettair" "$INSTALL_DIR/zettair-search"

### ── 10. Install systemd services ──────────────────────────────────────────

log "Installing systemd services..."
cp "$INSTALL_DIR/zettair-search/deploy/zettair-search.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable zettair-search

### ── 11. Install cloudflared ───────────────────────────────────────────────

log "Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
https://pkg.cloudflare.com/cloudflare $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/cloudflare.list
    apt-get update -qq && apt-get install -y -qq cloudflared
fi
cp "$INSTALL_DIR/zettair-search/deploy/cloudflared.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable cloudflared

### ── Done ───────────────────────────────────────────────────────────────────

log ""
log "═══════════════════════════════════════════════════════"
log "  Setup complete!"
log ""
log "  Next steps:"
log "  1. Copy tunnel credentials:"
log "       mkdir -p /etc/cloudflared"
log "       scp ~/.cloudflared/<tunnel-id>.json root@VPS:/etc/cloudflared/"
log ""
log "  2. Create /etc/cloudflared/config.yml:"
log "       tunnel: <tunnel-id>"
log "       credentials-file: /etc/cloudflared/<tunnel-id>.json"
log "       protocol: http2"
log "       ingress:"
log "         - hostname: search.yourdomain.com"
log "           service: http://localhost:8765"
log "         - service: http_status:404"
log ""
log "  3. Start services:"
log "       systemctl start zettair-search cloudflared"
log ""
log "  4. Check status:"
log "       systemctl status zettair-search"
log "       curl http://localhost:8765/search?q=test"
log "═══════════════════════════════════════════════════════"
