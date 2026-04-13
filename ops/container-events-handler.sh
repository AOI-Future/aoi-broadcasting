#!/usr/bin/env bash
# container-events-handler.sh
# Docker events を監視し、コンテナ障害を即時検知して復旧する
# GitHub Actions の on: event トリガーに相当する仕組み
#
# 対象イベント:
#   health_status: unhealthy  → ffmpeg ハング / プロセス異常
#   die                       → コンテナ予期終了（ログのみ）

set -uo pipefail

COMPOSE_DIR="$HOME/services/aoi-broadcasting"
LOG_FILE="$COMPOSE_DIR/ops/events-handler.log"
DISCORD_WEBHOOK_URL="$(grep '^DISCORD_WEBHOOK_URL=' "$HOME/clawd/.env" 2>/dev/null | cut -d= -f2- || echo '')"
COOLDOWN_SECS=300   # 再起動後 5 分間は同一チャンネルの再トリガーを抑制

log()    { printf '%s [events] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }
notify() {
    log "$1"
    [[ -z "${DISCORD_WEBHOOK_URL:-}" ]] && return 0
    local msg
    msg=$(printf '%s' "$1" | sed 's/"/\\"/g' | tr '\n' ' ')
    curl -s -X POST -H 'Content-Type: application/json' \
        -d "{\"content\": \"$msg\"}" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ファイルベースのクールダウン（while ループはサブシェルのため変数共有不可）
cooldown_file() { printf '/tmp/.aoi-events-cooldown-%s' "$1"; }
in_cooldown() {
    local f ts
    f=$(cooldown_file "$1")
    [[ ! -f "$f" ]] && return 1
    ts=$(cat "$f" 2>/dev/null || echo 0)
    (( $(date +%s) - ts < COOLDOWN_SECS ))
}
set_cooldown() { date +%s > "$(cooldown_file "$1")"; }

do_restart() {
    local ch="$1" reason="$2"
    set_cooldown "$ch"
    log "→ Restarting ${ch}: ${reason}"
    if cd "$COMPOSE_DIR" \
        && docker compose rm -sf "$ch" >> "$LOG_FILE" 2>&1 \
        && docker compose up -d "$ch" >> "$LOG_FILE" 2>&1; then
        notify "♻️ **NICTIA Radio ${ch}**: ${reason} を検知 → コンテナ再作成完了"
    else
        notify "🚨 **NICTIA Radio ${ch}**: ${reason} → 再作成失敗（手動確認が必要）"
    fi
}

log "=== container-events-handler started (PID $$) ==="

# Docker events を JSON でストリーム受信し Python3 でパース
# (bash の IFS 分割では health_status: unhealthy の ": " が扱いにくいため)
docker events \
    --filter 'name=aoi-broadcasting-ch1' \
    --filter 'name=aoi-broadcasting-ch2' \
    --filter 'event=die' \
    --filter 'event=health_status' \
    --format '{{json .}}' \
| while IFS= read -r line; do
    # JSON から status と name を抽出
    read -r status name <<< "$(printf '%s' "$line" | python3 -c "
import json, sys
e = json.loads(sys.stdin.read())
print(e.get('status',''), e.get('Actor',{}).get('Attributes',{}).get('name',''))
" 2>/dev/null || echo ' ')"

    [[ -z "$name" ]] && continue
    ch="${name#aoi-broadcasting-}"   # "ch1" or "ch2"

    case "$status" in
        'health_status: unhealthy')
            if in_cooldown "$ch"; then
                log "${name}: unhealthy (クールダウン中のためスキップ)"
                continue
            fi
            do_restart "$ch" "Docker healthcheck unhealthy"
            ;;
        die)
            # Docker restart policy (restart: unless-stopped) が自動で起動し直す。
            # ここでは可視化のためのみログ。二重起動を防ぐため restart はしない。
            log "${name}: コンテナが終了しました（Docker restart policy が処理）"
            ;;
    esac
done

log "docker events ストリームが終了 — systemd が再起動します"
