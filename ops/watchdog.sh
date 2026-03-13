#!/usr/bin/env bash
# aoi-broadcasting (Ch1) stream watchdog
# cron: */5 * * * * /home/shugo/services/aoi-broadcasting/ops/watchdog.sh >> /home/shugo/services/aoi-broadcasting/ops/watchdog.log 2>&1

set -uo pipefail

CONTAINER="aoi-broadcasting"
COMPOSE_DIR="$HOME/services/aoi-broadcasting"
DISCORD_WEBHOOK_URL="$(grep DISCORD_WEBHOOK_URL "$HOME/clawd/.env" 2>/dev/null | cut -d= -f2-)"
LOG_PREFIX="[broadcasting-1]"
YT_DLP="/home/linuxbrew/.linuxbrew/bin/yt-dlp"

YOUTUBE_WATCH_URL="$(grep YOUTUBE_WATCH_URL "$COMPOSE_DIR/.env" 2>/dev/null | cut -d= -f2-)"
KICK_CHANNEL_URL="$(grep KICK_CHANNEL_URL "$COMPOSE_DIR/.env" 2>/dev/null | cut -d= -f2-)"

LV1_FAIL_STATE="$COMPOSE_DIR/ops/.watchdog-fail-lv1"
LV2_FAIL_STATE="$COMPOSE_DIR/ops/.watchdog-fail-lv2"
LV2_RESTART_COUNT_FILE="$COMPOSE_DIR/ops/.watchdog-lv2-restarts"
COOLDOWN_FILE="$COMPOSE_DIR/ops/.watchdog-cooldown"
LOG_TS_FILE="$COMPOSE_DIR/ops/.last-log-ts"
LV2_INCONCLUSIVE_STATE="$COMPOSE_DIR/ops/.watchdog-lv2-inconclusive"
LOG_STALE_SECS=600  # 10分
COOLDOWN_SECS=900   # 再起動後15分はLv2チェック抑制
LV2_MAX_RESTARTS=2  # Lv2起因の再起動は最大2回/日。超過後は通知のみ
LV2_RESTART_RESET_SECS=86400  # 24時間でカウンタリセット

log()    { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"; }
notify() {
    local msg="$1"
    log "$msg"
    if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
        local escaped
        escaped=$(printf '%s' "$msg" | sed 's/"/\\"/g' | tr '\n' ' ')
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"content\": \"$escaped\"}" \
            "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1 || true
    fi
}

get_fail_count() { cat "$1" 2>/dev/null || echo 0; }
set_fail_count() { echo "$2" > "$1"; }
reset_fail()     { rm -f "$1"; }

in_cooldown() {
    if [ ! -f "$COOLDOWN_FILE" ]; then return 1; fi
    local cooldown_ts now elapsed
    cooldown_ts=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - cooldown_ts))
    if [ "$elapsed" -lt "$COOLDOWN_SECS" ]; then
        log "In cooldown (${elapsed}s / ${COOLDOWN_SECS}s since last restart). Skipping Lv2."
        return 0
    fi
    rm -f "$COOLDOWN_FILE"
    return 1
}

start_cooldown() { date +%s > "$COOLDOWN_FILE"; }

# RTMP接続状態を取得（診断情報用）
rtmp_status() {
    local connected
    connected=$(docker exec "$CONTAINER" python3 -c "
for fname in ['/proc/net/tcp', '/proc/net/tcp6']:
    try:
        with open(fname) as f:
            for line in f:
                p = line.split()
                if len(p) < 4: continue
                if p[3] == '01':
                    rport = int(p[2].split(':')[1], 16)
                    if rport in [1935, 443]:
                        print(f'port {rport}')
    except: pass
" 2>/dev/null | paste -sd ',' -)
    echo "${connected:-なし}"
}

# 最後のフレーム番号を取得（診断情報用）
last_frame() {
    docker logs --tail=5 "$CONTAINER" 2>&1 \
        | grep -oP 'frame=\s*\K[0-9]+' | tail -1 || echo "不明"
}

do_restart() {
    local reason="$1"
    log "Restarting ($reason)..."
    cd "$COMPOSE_DIR" && docker compose restart streamer >> "$COMPOSE_DIR/ops/watchdog.log" 2>&1
    rm -f "$LOG_TS_FILE"
    start_cooldown
}

set_inconclusive_marker() { date +%s > "$LV2_INCONCLUSIVE_STATE"; }
clear_inconclusive_marker() { rm -f "$LV2_INCONCLUSIVE_STATE"; }
should_notify_inconclusive() {
    if [ ! -f "$LV2_INCONCLUSIVE_STATE" ]; then
        return 0
    fi
    local last_ts now
    last_ts=$(cat "$LV2_INCONCLUSIVE_STATE" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $((now - last_ts)) -ge 21600 ]
}

# Migrate old fail state file
rm -f "$COMPOSE_DIR/ops/.watchdog-fail"

# ─── 1. コンテナ起動確認 ───
STATUS=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
if [ "$STATUS" != "running" ]; then
    log "Container is $STATUS. Starting..."
    cd "$COMPOSE_DIR" && docker compose up -d >> "$COMPOSE_DIR/ops/watchdog.log" 2>&1
    NEW_STATUS=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    if [ "$NEW_STATUS" = "running" ]; then
        notify "♻️ **NICTIA Radio Ch1**: コンテナ停止を検知→起動しました
📋 検知内容: コンテナ状態 = \`$STATUS\`"
        reset_fail "$LV1_FAIL_STATE"
        reset_fail "$LV2_FAIL_STATE"
        clear_inconclusive_marker
        rm -f "$LOG_TS_FILE" "$LV2_RESTART_COUNT_FILE"
        start_cooldown
    else
        notify "🚨 **NICTIA Radio Ch1**: コンテナ起動失敗。手動確認が必要です"
    fi
    exit 0
fi

# ─── 2. ffmpegハング検知（ログ更新停止チェック） ───
RECENT_LOG=$(docker logs --since="${LOG_STALE_SECS}s" "$CONTAINER" 2>&1 | wc -c)

if [ "$RECENT_LOG" -gt 0 ]; then
    echo "$(date +%s)" > "$LOG_TS_FILE"
    log "ffmpeg OK (log active, ${RECENT_LOG} bytes in last ${LOG_STALE_SECS}s)"
    reset_fail "$LV1_FAIL_STATE"
else
    LAST_TS=$(cat "$LOG_TS_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_TS))
    FAIL_COUNT=$(get_fail_count "$LV1_FAIL_STATE")

    if [ "$FAIL_COUNT" -eq 0 ]; then
        log "ffmpeg log stale (${ELAPSED}s). Will recheck next cycle."
        set_fail_count "$LV1_FAIL_STATE" 1
        exit 0
    fi

    # 診断情報を収集してから再起動
    RTMP=$(rtmp_status)
    FRAME=$(last_frame)
    ELAPSED_MIN=$(( ELAPSED / 60 ))

    notify "⚠️ **NICTIA Radio Ch1**: ffmpegが応答停止 → 再起動します
📋 検知内容: ffmpegログが約${ELAPSED_MIN}分間 無更新（閾値: ${LOG_STALE_SECS}秒×2サイクル）
🔌 RTMP接続: ${RTMP}（TCP keepaliveで維持されていても内部ハングの可能性あり）
🎞️ 最終フレーム: ${FRAME}
⚙️ 対応: docker compose restart streamer"
    do_restart "ffmpeg log stale"
    reset_fail "$LV1_FAIL_STATE"
    reset_fail "$LV2_FAIL_STATE"
    clear_inconclusive_marker
    rm -f "$LV2_RESTART_COUNT_FILE"
    exit 0
fi

# ─── 3. Lv2: 視聴者側到達確認（URLが設定済みの場合のみ） ───
# クールダウン中はスキップ
if in_cooldown; then
    exit 0
fi

LV2_FAIL=0
LV2_INCONCLUSIVE=0
LV2_DETAILS=""
LV2_INCONCLUSIVE_DETAILS=""

check_youtube() {
    local url="$1"
    if [ -z "$url" ]; then return 0; fi
    local output
    output=$("$YT_DLP" --no-warnings --simulate \
        --live-from-start --playlist-items 1 \
        "$url" 2>&1)
    local rc=$?
    if [ $rc -eq 0 ] || echo "$output" | grep -qi "begin in a few moments\|premieres in"; then
        log "Lv2 YouTube: stream reachable ✅"
        return 0
    else
        local reason
        reason=$(echo "$output" | grep -i "ERROR\|This\|is not" | head -1 | cut -c1-80)
        if echo "$output" | grep -qi "This live stream recording is not available\|video is unavailable\|This video isn't available anymore"; then
            LV2_INCONCLUSIVE_DETAILS="${LV2_INCONCLUSIVE_DETAILS}YouTube: ⚠️ watch URL が終了/無効の可能性 (${reason:-取得失敗}) / "
            log "Lv2 YouTube: watch URL appears stale/inactive ⚠️ ($reason)"
            return 2
        fi
        LV2_DETAILS="${LV2_DETAILS}YouTube: ❌ ${reason:-取得失敗} / "
        log "Lv2 YouTube: NOT reachable ❌ ($reason)"
        return 1
    fi
}

check_kick() {
    local url="$1"
    if [ -z "$url" ]; then return 0; fi
    local output
    output=$("$YT_DLP" --no-warnings --simulate --playlist-items 1 "$url" 2>&1)
    local rc=$?
    if [ $rc -eq 0 ]; then
        log "Lv2 Kick: stream reachable ✅"
        return 0
    else
        local reason
        reason=$(echo "$output" | grep -i "ERROR\|not live\|offline" | head -1 | cut -c1-80)
        if echo "$output" | grep -qi "HTTP Error 403\|Forbidden"; then
            LV2_INCONCLUSIVE_DETAILS="${LV2_INCONCLUSIVE_DETAILS}Kick: ⚠️ 外形監視が403で失敗 (${reason:-取得失敗}) / "
            log "Lv2 Kick: probe blocked/inconclusive ⚠️ ($reason)"
            return 2
        fi
        LV2_DETAILS="${LV2_DETAILS}Kick: ❌ ${reason:-取得失敗} / "
        log "Lv2 Kick: NOT reachable ❌ ($reason)"
        return 1
    fi
}

if [ -n "${YOUTUBE_WATCH_URL:-}" ]; then
    check_youtube "$YOUTUBE_WATCH_URL"
    case $? in
        1) LV2_FAIL=$((LV2_FAIL + 1)) ;;
        2) LV2_INCONCLUSIVE=$((LV2_INCONCLUSIVE + 1)) ;;
    esac
fi
if [ -n "${KICK_CHANNEL_URL:-}" ]; then
    check_kick "$KICK_CHANNEL_URL"
    case $? in
        1) LV2_FAIL=$((LV2_FAIL + 1)) ;;
        2) LV2_INCONCLUSIVE=$((LV2_INCONCLUSIVE + 1)) ;;
    esac
fi

RTMP="$(rtmp_status)"

if [ "$LV2_FAIL" -eq 0 ] && [ "$LV2_INCONCLUSIVE" -gt 0 ] && [ "$RTMP" != "なし" ]; then
    log "Lv2 checks inconclusive, but RTMP is connected (${RTMP}). Treating as monitoring issue, not delivery failure."
    reset_fail "$LV2_FAIL_STATE"
    if should_notify_inconclusive; then
        notify "ℹ️ **NICTIA Radio Ch1**: 配信停止ではなく、外形監視の判定が不確実です
📋 検知内容: ${LV2_INCONCLUSIVE_DETAILS}
🔌 RTMP接続: ${RTMP}
💡 現在の stream key で ingest 接続は成立しています。YOUTUBE_WATCH_URL の更新、または Kick 側の 403 回避が必要です"
        set_inconclusive_marker
    fi
    exit 0
fi

if [ "$LV2_FAIL" -gt 0 ]; then
    FAIL_COUNT=$(get_fail_count "$LV2_FAIL_STATE")
    FAIL_COUNT=$((FAIL_COUNT + 1))
    set_fail_count "$LV2_FAIL_STATE" "$FAIL_COUNT"

    if [ "$FAIL_COUNT" -lt 3 ]; then
        # 初回のみ通知、2回目はサイレント
        if [ "$FAIL_COUNT" -eq 1 ]; then
            notify "👁️ **NICTIA Radio Ch1**: 視聴者側チェックで異常を検知（様子見中）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
⏳ 3サイクル連続（約15分）で異常なら再起動します"
        else
            log "Lv2 fail count: $FAIL_COUNT/3 (waiting)"
        fi
        exit 0
    fi

    # 3サイクル連続失敗 → 再起動（ただし上限あり）
    FRAME=$(last_frame)

    # Lv2再起動カウンタを確認（無限再起動ループ防止）
    LV2_RESTART_TS=0
    LV2_RESTARTS=0
    if [ -f "$LV2_RESTART_COUNT_FILE" ]; then
        LV2_RESTART_TS=$(head -1 "$LV2_RESTART_COUNT_FILE" 2>/dev/null || echo 0)
        LV2_RESTARTS=$(tail -1 "$LV2_RESTART_COUNT_FILE" 2>/dev/null || echo 0)
        # 24時間経過でリセット
        NOW_TS=$(date +%s)
        if [ $((NOW_TS - LV2_RESTART_TS)) -ge "$LV2_RESTART_RESET_SECS" ]; then
            LV2_RESTARTS=0
            LV2_RESTART_TS=$NOW_TS
        fi
    fi

    if [ "$LV2_RESTARTS" -ge "$LV2_MAX_RESTARTS" ]; then
        # 再起動上限到達 → 通知のみ（再起動しない）
        notify "🔇 **NICTIA Radio Ch1**: 視聴者側から到達不可が継続中（再起動上限${LV2_MAX_RESTARTS}回/日に到達済み）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
🎞️ 最終フレーム: ${FRAME}
💡 RTMP接続が確立中の場合、YouTube/Kick側のライブイベント設定を確認してください"
        reset_fail "$LV2_FAIL_STATE"
    else
        # 再起動実行
        LV2_RESTARTS=$((LV2_RESTARTS + 1))
        printf '%s\n%s\n' "$(date +%s)" "$LV2_RESTARTS" > "$LV2_RESTART_COUNT_FILE"
        notify "🚨 **NICTIA Radio Ch1**: 視聴者側から3サイクル連続で到達不可 → 再起動します（${LV2_RESTARTS}/${LV2_MAX_RESTARTS}回目）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
🎞️ 最終フレーム: ${FRAME}
⚙️ 対応: docker compose restart streamer"
        do_restart "viewer-side unreachable"
        reset_fail "$LV2_FAIL_STATE"
        clear_inconclusive_marker
    fi
else
    # Lv2 OK → カウンタリセット
    reset_fail "$LV2_FAIL_STATE"
    clear_inconclusive_marker
fi
