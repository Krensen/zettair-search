#!/usr/bin/env bash
# deploy.sh — thin wrapper around setup.sh, called by CI on every push.
#
# Pulls the latest zettair-search FIRST, then runs the freshly-pulled
# setup.sh. If we let setup.sh do its own pull mid-run, bash would be
# executing the old in-memory copy of the script — fixes wouldn't take
# effect until the *next* run, which is how we got bitten on 15:05
# (commit d48f538 fixed a permissions bug but the run that pulled it
# was still executing the buggy pre-pull version).
#
# setup.sh itself is idempotent and staleness-aware: rebuilds zet only
# if C source changed, reindexes only if TREC changed, etc. Running it
# on a no-op change is fast (a few seconds).

set -euo pipefail

SEARCH_DIR=/opt/zettair-search

log() { echo "$(date '+%H:%M:%S') ── $*"; }

log "Pulling latest zettair-search..."
cd "$SEARCH_DIR"
git pull origin main

log "Running setup.sh (idempotent)..."
sudo bash "$SEARCH_DIR/deploy/setup.sh"
log "Deploy complete."
