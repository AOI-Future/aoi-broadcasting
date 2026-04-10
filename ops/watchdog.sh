#!/usr/bin/env bash
# aoi-broadcasting stream watchdog (multi-channel)
# Usage: watchdog.sh <channel>  (e.g., watchdog.sh ch1)
# cron:
#   */5 * * * * /home/shugo/services/aoi-broadcasting/ops/watchdog.sh ch1 >> /home/shugo/services/aoi-broadcasting/ops/watchdog-ch1.log 2>&1
#   */5 * * * * /home/shugo/services/aoi-broadcasting/ops/watchdog.sh ch2 >> /home/shugo/services/aoi-broadcasting/ops/watchdog-ch2.log 2>&1

set -uo pipefail

CHANNEL="${1:?Usage: watchdog.sh <channel> (e.g., ch1, ch2)}"
CONTAINER="aoi-broadcasting-${CHANNEL}"
COMPOSE_DIR="$HOME/services/aoi-broadcasting"
COMPOSE_SERVICE="$CHANNEL"
DISCORD_WEBHOOK_URL="$(grep DISCORD_WEBHOOK_URL "$HOME/clawd/.env" 2>/dev/null | cut -d= -f2-)"
LOG_PREFIX="[broadcasting-${CHANNEL}]"
YT_DLP="/home/shugo/.local/share/yt-dlp-venv/bin/yt-dlp"

ENV_FILE="$COMPOSE_DIR/.env.${CHANNEL}"
YOUTUBE_WATCH_URL="$(grep YOUTUBE_WATCH_URL "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
YOUTUBE_CHANNEL_ID="$(grep YOUTUBE_CHANNEL_ID "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
KICK_CHANNEL_URL="$(grep KICK_CHANNEL_URL "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"

ASSETS_DIR="$COMPOSE_DIR/assets/${CHANNEL}"

# ─── 0. 背景画像 PNG→JPG 自動変換 ───
# PNG背景はffmpegのloop動画生成でcolor_range=unknownになり
# YouTubeが映像として認識できない問題の根本対策
ensure_jpg_background() {
    local png_file jpg_file
    png_file=$(find "$ASSETS_DIR" -maxdepth 1 -name "*.png" ! -name "*.disabled" ! -name "*.bak*" -print -quit 2>/dev/null)
    jpg_file="$ASSETS_DIR/background.jpg"

    if [ -z "$png_file" ]; then return 0; fi

    # JPGが既に存在し、PNGより新しければスキップ
    if [ -f "$jpg_file" ] && [ "$jpg_file" -nt "$png_file" ]; then return 0; fi

    log "PNG背景を検知: $(basename "$png_file") → JPGに変換します"
    if python3 -c "
from PIL import Image
import sys
img = Image.open(sys.argv[1])
img.convert('RGB').save(sys.argv[2], quality=90)
" "$png_file" "$jpg_file" 2>/dev/null; then
        mv "$png_file" "${png_file}.disabled"
        log "背景画像を変換しました: $(basename "$png_file") → background.jpg"
        notify "🖼️ **NICTIA Radio ${CHANNEL}**: PNG背景をJPGに自動変換しました → コンテナ再作成します"
        do_restart "PNG→JPG background conversion"
        exit 0
    else
        log "WARNING: PNG→JPG変換に失敗しました"
    fi
}

LV1_FAIL_STATE="$COMPOSE_DIR/ops/.watchdog-fail-lv1-${CHANNEL}"
LV2_FAIL_STATE="$COMPOSE_DIR/ops/.watchdog-fail-lv2-${CHANNEL}"
LV2_RESTART_COUNT_FILE="$COMPOSE_DIR/ops/.watchdog-lv2-restarts-${CHANNEL}"
COOLDOWN_FILE="$COMPOSE_DIR/ops/.watchdog-cooldown-${CHANNEL}"
LOG_TS_FILE="$COMPOSE_DIR/ops/.last-log-ts-${CHANNEL}"
LV2_INCONCLUSIVE_STATE="$COMPOSE_DIR/ops/.watchdog-lv2-inconclusive-${CHANNEL}"
WAS_LIVE_RECOVERY_FILE="$COMPOSE_DIR/ops/.watchdog-was-live-recovery-${CHANNEL}"
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

# コンテナ完全再作成（restart ではなく rm + up で RTMP 接続を新規確立）
# 再作成後にYouTubeのGo Liveを最大3分ポーリングし、成功ならWATCH_URLを自動更新
# 戻り値: 0=再作成+YouTubeライブ確認, 1=再作成したがライブ未確認
do_restart() {
    local reason="$1"
    log "Recreating container ($reason)..."
    cd "$COMPOSE_DIR" \
        && docker compose rm -sf $COMPOSE_SERVICE >> "$COMPOSE_DIR/ops/watchdog-${CHANNEL}.log" 2>&1 \
        && docker compose up -d $COMPOSE_SERVICE >> "$COMPOSE_DIR/ops/watchdog-${CHANNEL}.log" 2>&1
    rm -f "$LOG_TS_FILE"
    start_cooldown

    # Post-restart: YouTubeがGo Liveするまでポーリング（最大180秒, 30秒間隔）
    if [ -z "${YOUTUBE_CHANNEL_ID:-}" ]; then return 1; fi
    log "Post-restart: waiting for YouTube Go Live (polling channel /live, max 180s)..."
    local attempt max_attempts=6 poll_interval=30
    for attempt in $(seq 1 $max_attempts); do
        sleep $poll_interval
        local output new_id live_status
        output=$("$YT_DLP" --no-warnings -j "https://www.youtube.com/channel/${YOUTUBE_CHANNEL_ID}/live" 2>&1)
        if [ $? -ne 0 ]; then
            log "Post-restart poll ${attempt}/${max_attempts}: yt-dlp failed, retrying..."
            continue
        fi
        live_status=$(echo "$output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('live_status','unknown'))" 2>/dev/null || echo "unknown")
        if [ "$live_status" = "is_live" ]; then
            new_id=$(echo "$output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
            log "Post-restart poll ${attempt}/${max_attempts}: YouTube is live ✅ (id=${new_id})"
            # WATCH_URL を自動更新
            if [ -n "$new_id" ] && [ -n "$ENV_FILE" ]; then
                local new_url="https://youtube.com/live/${new_id}"
                sed -i "s|^YOUTUBE_WATCH_URL=.*|YOUTUBE_WATCH_URL=${new_url}|" "$ENV_FILE"
                log "Auto-updated YOUTUBE_WATCH_URL → ${new_url}"
                notify "✅ **NICTIA Radio ${CHANNEL}**: コンテナ再作成後、YouTubeライブ復旧を確認
📋 新WATCH_URL: ${new_url}
⏱️ Go Live まで: $((attempt * poll_interval))秒"
            fi
            clear_was_live_recovery
            return 0
        fi
        log "Post-restart poll ${attempt}/${max_attempts}: live_status=${live_status}, retrying..."
    done
    log "Post-restart: YouTube Go Live not detected within $((max_attempts * poll_interval))s"
    return 1
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

# was_live 復旧済みマーカー: 再作成後に旧 URL が stale なのは既知
# TTL: 30分後に自動解除（永続化防止）
WAS_LIVE_RECOVERY_TTL=1800
mark_was_live_recovery() { date +%s > "$WAS_LIVE_RECOVERY_FILE"; }
clear_was_live_recovery() { rm -f "$WAS_LIVE_RECOVERY_FILE"; }
in_was_live_recovery() {
    [ -f "$WAS_LIVE_RECOVERY_FILE" ] || return 1
    local marker_ts now elapsed
    marker_ts=$(cat "$WAS_LIVE_RECOVERY_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - marker_ts))
    if [ "$elapsed" -ge "$WAS_LIVE_RECOVERY_TTL" ]; then
        log "was_live recovery TTL expired (${elapsed}s >= ${WAS_LIVE_RECOVERY_TTL}s). Clearing marker."
        clear_was_live_recovery
        return 1
    fi
    return 0
}

# チャンネルの /live エンドポイントで直接ライブ状態を確認（WATCH_URLが古い場合のフォールバック）
check_youtube_channel_live() {
    local channel_id="$1"
    if [ -z "$channel_id" ]; then return 2; fi

    local output
    output=$("$YT_DLP" --no-warnings --flat-playlist -j "https://www.youtube.com/channel/${channel_id}/live" 2>&1)
    local rc=$?

    if [ $rc -eq 0 ] && echo "$output" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('live_status')=='is_live' else 1)" 2>/dev/null; then
        local new_id
        new_id=$(echo "$output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        log "Lv2 YouTube channel /live: stream is live ✅ (id=${new_id})"
        # WATCH_URL を自動更新
        if [ -n "$new_id" ] && [ -n "$ENV_FILE" ]; then
            local new_url="https://youtube.com/live/${new_id}"
            local current_url
            current_url=$(grep YOUTUBE_WATCH_URL "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
            if [ "$current_url" != "$new_url" ]; then
                sed -i "s|^YOUTUBE_WATCH_URL=.*|YOUTUBE_WATCH_URL=${new_url}|" "$ENV_FILE"
                log "Auto-updated YOUTUBE_WATCH_URL: ${current_url} → ${new_url}"
                notify "🔄 **NICTIA Radio ${CHANNEL}**: YOUTUBE_WATCH_URL を自動更新しました
📋 旧URL: ${current_url}
📋 新URL: ${new_url}"
            fi
        fi
        clear_was_live_recovery
        return 0
    fi

    if echo "$output" | grep -qi "not currently live"; then
        log "Lv2 YouTube channel /live: channel is NOT live ❌"
        LV2_DETAILS="${LV2_DETAILS}YouTube(channel): ❌ チャンネルがライブ配信中でない / "
        return 1
    fi

    log "Lv2 YouTube channel /live: inconclusive (rc=$rc)"
    return 2
}

# Migrate old fail state files
rm -f "$COMPOSE_DIR/ops/.watchdog-fail" "$COMPOSE_DIR/ops/.watchdog-fail-${CHANNEL}"


# ─── 0. PNG背景の自動変換 ───
ensure_jpg_background

# ─── 1. コンテナ起動確認 ───
STATUS=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
if [ "$STATUS" != "running" ]; then
    log "Container is $STATUS. Starting..."
    cd "$COMPOSE_DIR" && docker compose up -d "$COMPOSE_SERVICE" >> "$COMPOSE_DIR/ops/watchdog-${CHANNEL}.log" 2>&1
    NEW_STATUS=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    if [ "$NEW_STATUS" = "running" ]; then
        notify "♻️ **NICTIA Radio ${CHANNEL}**: コンテナ停止を検知→起動しました
📋 検知内容: コンテナ状態 = \`$STATUS\`"
        reset_fail "$LV1_FAIL_STATE"
        reset_fail "$LV2_FAIL_STATE"
        clear_inconclusive_marker
        clear_was_live_recovery
        rm -f "$LOG_TS_FILE" "$LV2_RESTART_COUNT_FILE"
        start_cooldown
    else
        notify "🚨 **NICTIA Radio ${CHANNEL}**: コンテナ起動失敗。手動確認が必要です"
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

    notify "⚠️ **NICTIA Radio ${CHANNEL}**: ffmpegが応答停止 → コンテナ再作成します
📋 検知内容: ffmpegログが約${ELAPSED_MIN}分間 無更新（閾値: ${LOG_STALE_SECS}秒×2サイクル）
🔌 RTMP接続: ${RTMP}（TCP keepaliveで維持されていても内部ハングの可能性あり）
🎞️ 最終フレーム: ${FRAME}
⚙️ 対応: docker compose rm -sf / up -d $COMPOSE_SERVICE"
    do_restart "ffmpeg log stale"
    reset_fail "$LV1_FAIL_STATE"
    reset_fail "$LV2_FAIL_STATE"
    clear_inconclusive_marker
    clear_was_live_recovery
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

# YouTube ライブ状態を JSON で確認（--simulate ではVODと区別できないため）
check_youtube() {
    local url="$1"
    if [ -z "$url" ]; then return 0; fi

    local json_output live_status
    json_output=$("$YT_DLP" --no-warnings -j "$url" 2>&1)
    local rc=$?

    if [ $rc -ne 0 ]; then
        local reason
        reason=$(echo "$json_output" | grep -i "ERROR\|This\|is not" | head -1 | cut -c1-80)
        if echo "$json_output" | grep -qi "This live stream recording is not available\|video is unavailable\|This video isn't available anymore"; then
            # stale/終了済みURLはライブ終了と同等 → fail扱いでチャンネル /live フォールバックを発動させる
            LV2_DETAILS="${LV2_DETAILS}YouTube: ❌ watch URL が終了/無効 (${reason:-取得失敗}) / "
            log "Lv2 YouTube: watch URL stale/ended ❌ ($reason)"
            return 1
        fi
        LV2_DETAILS="${LV2_DETAILS}YouTube: ❌ ${reason:-取得失敗} / "
        log "Lv2 YouTube: NOT reachable ❌ ($reason)"
        return 1
    fi

    # JSON から live_status を取得
    live_status=$(echo "$json_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('live_status','unknown'))" 2>/dev/null || echo "unknown")

    case "$live_status" in
        is_live)
            log "Lv2 YouTube: stream is live ✅ (live_status=$live_status)"
            # ライブ確認できたら was_live 復旧マーカーをクリア
            clear_was_live_recovery
            return 0
            ;;
        was_live|post_live)
            # ライブ終了: YouTube はデータを受け付けるが配信しない
            LV2_DETAILS="${LV2_DETAILS}YouTube: ❌ ライブ終了 (live_status=${live_status}) / "
            log "Lv2 YouTube: live event ended (${live_status}) ❌"
            return 1
            ;;
        *)
            # 判定不能だがアクセスはできた
            log "Lv2 YouTube: accessible but live_status unclear (${live_status}) ✅"
            return 0
            ;;
    esac
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

if [ -n "${YOUTUBE_WATCH_URL:-}" ] || [ -n "${YOUTUBE_CHANNEL_ID:-}" ]; then
    if in_was_live_recovery; then
        # was_live recovery モード中: WATCH_URL はスキップするがチャンネル /live で確認
        log "Lv2 YouTube: WATCH_URL skipped (was_live recovery mode)"
        if [ -n "${YOUTUBE_CHANNEL_ID:-}" ]; then
            check_youtube_channel_live "$YOUTUBE_CHANNEL_ID"
            case $? in
                0) ;; # ライブ確認 → was_live_recovery 自動クリア済み
                1) LV2_FAIL=$((LV2_FAIL + 1)) ;;
                2) LV2_INCONCLUSIVE=$((LV2_INCONCLUSIVE + 1)) ;;
            esac
        else
            LV2_INCONCLUSIVE=$((LV2_INCONCLUSIVE + 1))
            LV2_INCONCLUSIVE_DETAILS="${LV2_INCONCLUSIVE_DETAILS}YouTube: ⚠️ was_live recovery中 + CHANNEL_ID未設定 / "
        fi
    else
        check_youtube "$YOUTUBE_WATCH_URL"
        yt_rc=$?
        case $yt_rc in
            0) ;; # OK
            1)
                # WATCH_URL でライブ終了検知 → チャンネル /live でフォールバック
                if [ -n "${YOUTUBE_CHANNEL_ID:-}" ]; then
                    log "Lv2 YouTube: WATCH_URL failed, trying channel /live fallback..."
                    check_youtube_channel_live "$YOUTUBE_CHANNEL_ID"
                    case $? in
                        0) ;; # チャンネルでライブ確認 → OK
                        1) LV2_FAIL=$((LV2_FAIL + 1)) ;;
                        2) LV2_FAIL=$((LV2_FAIL + 1)) ;; # WATCH_URLが失敗+チャンネルが不明確→失敗扱い
                    esac
                else
                    LV2_FAIL=$((LV2_FAIL + 1))
                fi
                ;;
            2) LV2_INCONCLUSIVE=$((LV2_INCONCLUSIVE + 1)) ;;
        esac
    fi
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
        notify "ℹ️ **NICTIA Radio ${CHANNEL}**: 配信停止ではなく、外形監視の判定が不確実です
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
            notify "👁️ **NICTIA Radio ${CHANNEL}**: 視聴者側チェックで異常を検知（様子見中）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
⏳ 3サイクル連続（約15分）で異常なら再起動します"
        else
            log "Lv2 fail count: $FAIL_COUNT/3 (waiting)"
        fi
        exit 0
    fi

    # 3サイクル連続失敗 → コンテナ再作成（ただし上限あり）
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
        notify "🔇 **NICTIA Radio ${CHANNEL}**: 視聴者側から到達不可が継続中（再起動上限${LV2_MAX_RESTARTS}回/日に到達済み）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
🎞️ 最終フレーム: ${FRAME}
💡 RTMP接続が確立中の場合、YouTube/Kick側のライブイベント設定を確認してください"
        reset_fail "$LV2_FAIL_STATE"
    else
        # コンテナ再作成実行
        # was_live が原因の場合、復旧後に旧 URL で再度検知しないようマーカーを設定
        if echo "$LV2_DETAILS" | grep -q "ライブ終了"; then
            mark_was_live_recovery
        fi

        notify "🚨 **NICTIA Radio ${CHANNEL}**: 視聴者側から3サイクル連続で到達不可 → コンテナ再作成します（${LV2_RESTARTS}/${LV2_MAX_RESTARTS}回目の枠を使用）
📋 検知内容: ${LV2_DETAILS}
🔌 RTMP接続: ${RTMP}
🎞️ 最終フレーム: ${FRAME}
⚙️ 対応: docker compose rm -sf / up -d $COMPOSE_SERVICE"
        do_restart "viewer-side unreachable"
        restart_rc=$?
        reset_fail "$LV2_FAIL_STATE"
        clear_inconclusive_marker

        if [ $restart_rc -eq 0 ]; then
            # Post-restartでYouTube Go Live確認済み → 再起動枠を消費しない
            log "Restart succeeded with YouTube Go Live confirmed. Not counting toward daily limit."
        else
            # YouTube Go Live未確認 → 再起動枠を消費
            LV2_RESTARTS=$((LV2_RESTARTS + 1))
            printf '%s\n%s\n' "$(date +%s)" "$LV2_RESTARTS" > "$LV2_RESTART_COUNT_FILE"
            log "Restart done but YouTube Go Live not confirmed. Restart count: ${LV2_RESTARTS}/${LV2_MAX_RESTARTS}"
        fi
    fi
else
    # Lv2 OK → カウンタリセット
    reset_fail "$LV2_FAIL_STATE"
    clear_inconclusive_marker
fi
