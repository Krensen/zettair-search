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

log "Installing Python dependencies..."
pip3 install --quiet -r requirements.txt

log "Restarting service..."
systemctl restart zettair-search

log "Waiting for server to come up..."
sleep 5
if curl -sf --max-time 5 http://localhost:8765/search?q=test > /dev/null; then
    log "✓ Server is up and responding"
else
    log "✗ Server health check failed — check: journalctl -u zettair-search -n 50"
    exit 1
fi

log "Deploy complete."
