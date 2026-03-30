#!/bin/bash
# watchdog.sh — checks zettair-search and cloudflared tunnel are healthy,
# kicks launchd to restart them if not.
# Designed to run every 5 minutes via launchd.

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

# ── Check 2: cloudflared has live tunnel connections ──
# We check for the process; a deeper check would hit the metrics API but
# this is sufficient since KeepAlive=true handles crashes already.
# The real failure mode is the tunnel connecting but the edge dropping it —
# so we check the log for a recent "Registered tunnel connection" line.
CLOUDFLARE_LOG=/Users/hughwilliams/.openclaw/agents/metabot/workspace/zettair-service/logs/cloudflared.log

if [ -f "$CLOUDFLARE_LOG" ]; then
    # Check if a successful connection was logged in the last 10 minutes
    RECENT=$(find "$CLOUDFLARE_LOG" -newer /tmp/.watchdog_mark 2>/dev/null | head -1)
    # Simpler: just check if the process is alive and has connections
    if pgrep -f "cloudflared tunnel run" > /dev/null 2>&1; then
        # Check log tail for ERR/no more connections pattern
        TAIL=$(tail -20 "$CLOUDFLARE_LOG" 2>/dev/null)
        if echo "$TAIL" | grep -q "no more connections active and exiting"; then
            log "FAIL cloudflared — detected dead tunnel, restarting"
            restart_service "com.cloudflared-zettair"
        elif echo "$TAIL" | grep -q "Registered tunnel connection"; then
            log "OK cloudflared"
        else
            log "WARN cloudflared — no recent connection lines, restarting to be safe"
            restart_service "com.cloudflared-zettair"
        fi
    else
        log "FAIL cloudflared — process not found, restarting"
        restart_service "com.cloudflared-zettair"
    fi
else
    log "WARN cloudflared — log file not found"
fi

trim_log
