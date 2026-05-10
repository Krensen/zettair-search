#!/usr/bin/env bash
# deploy.sh — thin wrapper around setup.sh, called by CI on every push.
#
# setup.sh is idempotent and staleness-aware: it pulls both repos,
# rebuilds zet only if the C source changed, rebuilds the index only if
# the TREC changed, rebuilds click_prior only if clickstream / docno_map
# changed, etc. Running it on a no-op change is fast (a few seconds).
#
# So deploy.sh just delegates. There is exactly one entry point on the
# box: `sudo bash setup.sh` (or this script, which is the CI face of it).

set -euo pipefail

SEARCH_DIR=/opt/zettair-search

log() { echo "$(date '+%H:%M:%S') ── $*"; }

log "Running setup.sh (idempotent)..."
sudo bash "$SEARCH_DIR/deploy/setup.sh"
log "Deploy complete."
