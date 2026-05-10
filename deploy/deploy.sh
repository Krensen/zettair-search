#!/usr/bin/env bash
# deploy.sh — pull latest code from GitHub and restart the server
# Called by GitHub Actions CI/CD on every push to main.
# Safe to run manually too.
#
# Usage: bash deploy.sh

set -euo pipefail

INSTALL_DIR=/opt
log() { echo "$(date '+%H:%M:%S') ── $*"; }

log "Deploying zettair-search..."
cd "$INSTALL_DIR/zettair-search"
git pull origin main

log "Pulling and rebuilding zettair (C engine)..."
cd "$INSTALL_DIR/zettair"
ZETTAIR_OLD=$(git rev-parse HEAD)
git pull origin main
ZETTAIR_NEW=$(git rev-parse HEAD)
if [ "$ZETTAIR_OLD" != "$ZETTAIR_NEW" ]; then
    log "  zettair changed ($ZETTAIR_OLD..$ZETTAIR_NEW) — rebuilding"
    cd "$INSTALL_DIR/zettair/devel"
    make -j"$(nproc)"
    # libtool leaves zet as a wrapper script that re-links on first
    # invocation; the zettair user can't write into deploy-owned .libs/.
    # Replace the wrapper with the real ELF binary built into .libs/.
    sudo cp "$INSTALL_DIR/zettair/devel/.libs/zet" "$INSTALL_DIR/zettair/devel/zet"
    sudo chown zettair:zettair "$INSTALL_DIR/zettair/devel/zet"
    log "  zet binary updated"
else
    log "  zettair already up to date"
fi
cd "$INSTALL_DIR/zettair-search"

log "Installing Python dependencies..."
pip3 install --quiet --break-system-packages -r requirements.txt

log "Restarting service..."
sudo systemctl restart zettair-search

log "Waiting for server to come up..."
sleep 5
if curl -sf --max-time 5 http://localhost:8765/search?q=test > /dev/null; then
    log "✓ Server is up and responding"
else
    log "✗ Server health check failed — check: journalctl -u zettair-search -n 50"
    exit 1
fi

log "Deploy complete."
