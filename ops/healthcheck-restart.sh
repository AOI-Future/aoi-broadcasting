#!/usr/bin/env bash
# Docker healthcheck-based auto-restart (案3: cron safety net)
# Watchdog (ops/watchdog.sh) とは独立した二重安全ネット
# cron: */3 * * * * /home/shugo/services/aoi-broadcasting/ops/healthcheck-restart.sh >> /home/shugo/services/aoi-broadcasting/ops/healthcheck-restart.log 2>&1
set -uo pipefail

COMPOSE_DIR="$HOME/services/aoi-broadcasting"
DISCORD_WEBHOOK_URL="$(grep DISCORD_WEBHOOK_URL "$HOME/clawd/.env" 2>/dev/null | cut -d= -f2-)"

log() { echo "$(date "+%Y-%m-%d %H:%M:%S") [healthcheck] $*"; }
notify() {
    log "$1"
    if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        local escaped
        escaped=$(printf %s "$1" | sed s//\/g | tr n  )
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"content\": \"$escaped\"}" \
            "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1 || true
    fi
}

for ch in ch1 ch2; do
    CONTAINER="aoi-broadcasting-${ch}"

    # コンテナが存在しない場合はスキップ
    STATUS=$(docker inspect -f {{.State.Status}} "$CONTAINER" 2>/dev/null || echo "missing")
    if [ "$STATUS" = "missing" ]; then
        continue
    fi

    HEALTH=$(docker inspect -f {{.State.Health.Status}} "$CONTAINER" 2>/dev/null || echo "none")

    case "$HEALTH" in
        healthy)
            log "${ch}: healthy ✅"
            ;;
        unhealthy)
            # unhealthy検知 → コンテナ再作成
            LAST_LOG=$(docker inspect -f {{(index .State.Health.Log 0).Output}} "$CONTAINER" 2>/dev/null | head -c 200)
            notify "🔧 **NICTIA Radio ${ch}**: Docker healthcheck unhealthy → コンテナ再作成します
📋 healthcheck出力: ${LAST_LOG:-N/A}
⚙️ 対応: docker compose rm -sf / up -d ${ch}"
            cd "$COMPOSE_DIR" \
                && docker compose rm -sf "$ch" >> "$COMPOSE_DIR/ops/healthcheck-restart.log" 2>&1 \
                && docker compose up -d "$ch" >> "$COMPOSE_DIR/ops/healthcheck-restart.log" 2>&1
            ;;
        starting)
            log "${ch}: starting (waiting for healthcheck)"
            ;;
        none)
            log "${ch}: no healthcheck configured (container status: ${STATUS})"
            ;;
        *)
            log "${ch}: unknown health status: ${HEALTH}"
            ;;
    esac
done
