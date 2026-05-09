#!/bin/bash
# watchdog.sh — checks zettair-search is healthy and kicks launchd to
# restart it if not. Designed to run every 5 minutes via launchd.

LOG=/Users/hughwilliams/.openclaw/agents/metabot/workspace/zettair-service/logs/watchdog.log
MAX_LOG_LINES=500
UID_VAL=$(id -u)

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"
}

# Trim log to last MAX_LOG_LINES lines
trim_log() {
    if [ -f "$LOG" ]; then
        local lines=$(wc -l < "$LOG")
        if [ "$lines" -gt "$MAX_LOG_LINES" ]; then
            tail -n "$MAX_LOG_LINES" "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
        fi
    fi
}

restart_service() {
    local label="$1"
    log "RESTART $label"
    launchctl kickstart -k "gui/${UID_VAL}/${label}" >> "$LOG" 2>&1
}

# ── Check 1: search server responds on port 8765 ──
if curl -sf --max-time 5 "http://localhost:8765/search?q=test&n=1" > /dev/null 2>&1; then
    log "OK zettair-search"
else
    log "FAIL zettair-search — restarting"
    restart_service "com.zettair-search"
fi

trim_log
