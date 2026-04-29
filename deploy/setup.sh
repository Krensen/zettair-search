#!/usr/bin/env bash
# setup.sh — VPS provisioning for zettair-search (Top-N enwiki corpus)
#
# Tested on: Ubuntu 24.04 ARM64 (Hetzner CCX13)
# Run as root or a user with sudo:
#
#   sudo bash deploy/setup.sh
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
# Every step is guarded by an existence check — safe to re-run after failure.

set -euo pipefail

### ── Config — edit these if you need to change anything ────────────────────

DEPLOY_USER=deploy                    # user that runs git pull and deploys code
SERVICE_USER=zettair                  # user that runs the search service

INSTALL_DIR=/opt
ZETTAIR_SEARCH_REPO=https://github.com/Krensen/zettair-search.git
ZETTAIR_REPO=https://github.com/Krensen/zettair.git

VOLUME=/mnt/wikipedia-source          # Hetzner volume mount point
CORPUS_SIZE=1500000                   # number of top articles to index

ENWIKI_DUMP_URL="https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-pages-articles.xml.bz2"
BZ2_DELETE_THRESHOLD_GB=30            # auto-delete bz2 after TREC if free space below this

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

# Run a command as the deploy user. Repo writes use this.
as_deploy() { sudo -u "$DEPLOY_USER" "$@"; }

# Run a command as the zettair (service) user. Volume writes use this.
as_zettair() { sudo -u "$SERVICE_USER" "$@"; }

### ── 1. System dependencies (root) ─────────────────────────────────────────

log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git gcc make autoconf automake libtool pkg-config \
    libz-dev curl wget

pip3 install --quiet --break-system-packages fastapi uvicorn

### ── 2. Create users (root) ─────────────────────────────────────────────────

log "Creating users..."
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
    log "  Created $DEPLOY_USER"
fi
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    log "  Created $SERVICE_USER"
fi

### ── 3. Verify volume mount and set ownership upfront (root) ────────────────

[ -d "$VOLUME" ] || die "$VOLUME not found — mount the Hetzner volume first"
log "Volume $VOLUME present."

# Set volume ownership ONCE, before anything writes to it. Subsequent writes
# are all done via `sudo -u zettair`, which creates files as zettair naturally.
chown "$SERVICE_USER:$SERVICE_USER" "$VOLUME"
chmod 750 "$VOLUME"

### ── 4. Clone repos (deploy) ───────────────────────────────────────────────

log "Cloning repos..."
# Make sure the install dir is traversable by deploy if it isn't already.
mkdir -p "$INSTALL_DIR"

# If repos exist, fix ownership in case a previous run created them as root.
# If repos don't exist, parent must be writable by deploy for the clone.
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR" 2>/dev/null || true
[ -d "$SEARCH_DIR" ] || as_deploy git clone "$ZETTAIR_SEARCH_REPO" "$SEARCH_DIR"
[ -d "$ZETTAIR_DIR" ] || as_deploy git clone "$ZETTAIR_REPO" "$ZETTAIR_DIR"

# World-readable so the zettair service user can read server.py and the zet binary
# without owning these directories. (deploy still owns; zettair just reads.)
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SEARCH_DIR" "$ZETTAIR_DIR"
chmod -R o+rX "$SEARCH_DIR" "$ZETTAIR_DIR"

# Logs dir for the search service must be writable by the service user.
mkdir -p "$SEARCH_DIR/logs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$SEARCH_DIR/logs"

### ── 5. Build Zettair binary (deploy) ──────────────────────────────────────
#
# After `make`, libtool leaves the binary as a wrapper script at devel/zet
# that re-links the real ELF binary on first invocation if it can't find a
# writable build tree. That breaks when a non-deploy user (zettair) runs it.
# We replace the wrapper with the real binary from .libs/ and make the
# shared library globally findable so zet runs regardless of CWD or invoker.

# Always re-check: $ZET_BIN may exist as the libtool wrapper from a previous run.
NEEDS_BUILD=1
if [ -f "$ZET_BIN" ] && file "$ZET_BIN" | grep -q ELF; then
    NEEDS_BUILD=0
fi

if [ "$NEEDS_BUILD" = "1" ]; then
    log "Building Zettair..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        BUILD_FLAG="--build=aarch64-unknown-linux-gnu"
    else
        BUILD_FLAG=""
    fi
    as_deploy bash -c "cd '$ZETTAIR_DIR/devel' && ./configure $BUILD_FLAG && make -j$(nproc)"

    # Replace the libtool wrapper with the real ELF binary.
    if [ -f "$ZETTAIR_DIR/devel/.libs/zet" ]; then
        cp "$ZETTAIR_DIR/devel/.libs/zet" "$ZET_BIN"
        chmod +x "$ZET_BIN"
        chown "$DEPLOY_USER:$DEPLOY_USER" "$ZET_BIN"
    fi

    # Register libzet.so so the bare binary can find it without LD_LIBRARY_PATH.
    if [ -f "$ZETTAIR_DIR/devel/.libs/libzet.so.0" ]; then
        ln -sf "$ZETTAIR_DIR/devel/.libs/libzet.so.0" /usr/local/lib/libzet.so.0
        ldconfig
    fi

    log "Binary installed at: $ZET_BIN"
else
    log "Zettair binary already built (real ELF) — skipping."
fi

### ── 6. Download enwiki bz2 dump (zettair, to volume) ──────────────────────

BZ2_MIN_SIZE=$((20 * 1024 * 1024 * 1024))  # 20 GB in bytes
if [ -f "$TREC_FILE" ]; then
    log "TREC file already exists — skipping bz2 download."
elif [ -f "$BZ2_FILE" ] && [ "$(stat -c%s "$BZ2_FILE")" -gt "$BZ2_MIN_SIZE" ]; then
    log "enwiki bz2 dump already present — skipping download."
else
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
        log "  Downloading clickstream $MONTH..."
        as_deploy wget -q --show-progress \
            "https://dumps.wikimedia.org/other/clickstream/${MONTH}/clickstream-enwiki-${MONTH}.tsv.gz" \
            -O "$FILE" || { log "  WARNING: $MONTH not available, skipping"; rm -f "$FILE"; }
        sleep 2
    fi
done

### ── 8. select_top_articles.py → top_titles.txt (zettair, to volume) ────────

if [ ! -f "$TITLES_FILE" ]; then
    log "Running select_top_articles.py (CORPUS_SIZE=$CORPUS_SIZE)..."
    as_zettair python3 "$WIKI_DIR/select_top_articles.py" \
        --top "$CORPUS_SIZE" --out "$TITLES_FILE"
    log "top_titles.txt written to $TITLES_FILE"
else
    log "top_titles.txt already exists — skipping select_top_articles.py."
fi

### ── 9. wiki2trec.py → TREC + sidecar files (zettair, to volume) ───────────

if [ ! -f "$TREC_FILE" ]; then
    log "Running wiki2trec.py (bz2 streaming + title allowlist, ~4-8 hours)..."
    as_zettair python3 "$WIKI_DIR/wiki2trec.py" \
        "$BZ2_FILE" "$TREC_FILE" --titles "$TITLES_FILE"
    log "TREC file written to $TREC_FILE"
else
    log "TREC file already exists — skipping wiki2trec.py."
fi

### ── 10. Delete bz2 if disk is tight (zettair) ─────────────────────────────

if [ -f "$BZ2_FILE" ]; then
    FREE_GB=$(df -BG "$VOLUME" | awk 'NR==2 {gsub("G",""); print $4}')
    if [ "$FREE_GB" -lt "$BZ2_DELETE_THRESHOLD_GB" ]; then
        log "Free space is ${FREE_GB}GB < ${BZ2_DELETE_THRESHOLD_GB}GB — deleting bz2..."
        as_zettair rm -f "$BZ2_FILE"
        log "bz2 deleted. Free space: $(df -BG "$VOLUME" | awk 'NR==2 {print $4}')"
    else
        log "Free space is ${FREE_GB}GB — keeping bz2."
    fi
fi

### ── 11. Pipeline: docno map, click prior, autosuggest, docstore, urls ─────

log "Building docno map..."
if [ ! -f "$WIKI_DIR/docno_map.tsv" ]; then
    # build_docno_map.py writes docno_map.tsv into its own directory
    as_deploy python3 "$WIKI_DIR/build_docno_map.py" "$TREC_FILE"
else
    log "  docno_map.tsv already exists — skipping."
fi

log "Extracting titles for autosuggest..."
if [ ! -f "$WIKI_DIR/enwiki_titles.txt" ]; then
    as_deploy bash -c "cut -f2 '$WIKI_DIR/docno_map.tsv' > '$WIKI_DIR/enwiki_titles.txt'"
fi

log "Building click prior..."
if [ ! -f "$VOLUME/click_prior.bin" ]; then
    # build_click_prior.py uses HERE-relative paths — must run with WIKI_DIR as CWD
    as_deploy bash -c "cd '$WIKI_DIR' && python3 build_click_prior.py"
    # Copy into the volume as zettair so the volume copy has correct ownership
    as_zettair cp "$WIKI_DIR/click_prior.bin" "$VOLUME/click_prior.bin"
    log "  click_prior.bin copied to $VOLUME"
else
    log "  click_prior.bin already exists — skipping."
fi

log "Building autosuggest index..."
if [ ! -f "$VOLUME/autosuggest.json" ]; then
    as_deploy bash -c "cd '$WIKI_DIR' && python3 build_autosuggest.py"
    as_zettair cp "$WIKI_DIR/autosuggest.json" "$VOLUME/autosuggest.json"
    log "  autosuggest.json copied to $VOLUME"
else
    log "  autosuggest.json already exists — skipping."
fi

log "Building docstore..."
if [ ! -f "$VOLUME/enwiki_top1m.docstore" ]; then
    # build_docstore.py derives output paths from the TREC path → writes to volume directly
    as_zettair python3 "$WIKI_DIR/build_docstore.py" "$TREC_FILE"
    log "  docstore written alongside TREC on $VOLUME"
else
    log "  enwiki_top1m.docstore already exists — skipping."
fi

log "Building URLs store..."
URLS_STORE="$VOLUME/enwiki_top1m_urls.store"
URLS_MAP="$VOLUME/enwiki_top1m_urls.map"
if [ ! -f "$URLS_STORE" ]; then
    # wiki2trec.py writes the urls store natively for fresh builds.
    # If it's missing here (e.g. the index pre-dates PRD-015), bootstrap from
    # a dbkeys.tsv file if one is present.
    DBKEYS_FILE="$VOLUME/enwiki_top1m.dbkeys.tsv"
    if [ -f "$DBKEYS_FILE" ]; then
        as_zettair python3 "$WIKI_DIR/build_urls_store.py" \
            "$DBKEYS_FILE" "$URLS_STORE" "$URLS_MAP"
    else
        log "  WARNING: no urls store and no dbkeys.tsv to bootstrap from — punctuation links will 404"
    fi
else
    log "  enwiki_top1m_urls.store already exists — skipping."
fi

### ── 12. Build Zettair index (zettair, to volume) ──────────────────────────

if [ ! -f "$INDEX_DIR/index.param.0" ]; then
    log "Building Zettair index (can take 30-60 min for 1M articles)..."
    as_zettair mkdir -p "$INDEX_DIR"
    as_zettair "$ZET_BIN" -i -f "$INDEX_DIR/index" "$TREC_FILE"
    log "Index built at $INDEX_DIR"
else
    log "Index already exists at $INDEX_DIR — skipping."
fi

### ── 13. Install systemd service (root) ────────────────────────────────────

log "Installing systemd service..."
cp "$SEARCH_DIR/deploy/zettair-search.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable zettair-search

### ── 14. Verify ownership (loud failure if anything is misowned) ───────────

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

### ── Done ───────────────────────────────────────────────────────────────────

log ""
log "═══════════════════════════════════════════════════════"
log "  Setup complete!"
log ""
log "  Cutover checklist:"
log "  1. Verify artifacts:"
log "       sudo -u $SERVICE_USER ls -lh $INDEX_DIR/index.param.0"
log "       sudo -u $SERVICE_USER ls -lh $VOLUME/enwiki_top1m.docstore"
log "       sudo -u $SERVICE_USER ls -lh $VOLUME/enwiki_top1m_snippets.store"
log "  2. Smoke-test the index:"
log "       sudo -u $SERVICE_USER $ZET_BIN -f $INDEX_DIR/index --summary=plain --output=json -n 3 <<< 'einstein'"
log "  3. Restart the service:"
log "       sudo systemctl restart zettair-search"
log "       sudo systemctl status zettair-search"
log "       curl 'http://localhost:8765/search?q=einstein'"
log "═══════════════════════════════════════════════════════"
