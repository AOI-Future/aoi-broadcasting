#!/usr/bin/env bash
# setup-systemd.sh — cron を廃止し systemd units をインストールする
# Usage: ./ops/setup-systemd.sh
# 冪等: 何度実行しても安全

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/systemd"
UNIT_DST="$HOME/.config/systemd/user"

log() { printf '[setup-systemd] %s\n' "$*"; }

# ── 1. Unit ファイルをインストール ──────────────────────────────
mkdir -p "$UNIT_DST"
for f in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer; do
    [[ -f "$f" ]] || continue
    cp "$f" "$UNIT_DST/"
    log "installed: $(basename "$f")"
done

systemctl --user daemon-reload
log "daemon-reload done"

# ── 2. イベント監視サービスを起動 ──────────────────────────────
systemctl --user enable --now aoi-broadcasting-monitor.service
log "monitor service: enabled + started"

# ── 3. YouTube live チェック タイマーを起動 ────────────────────
for ch in ch1 ch2; do
    systemctl --user enable --now "aoi-broadcasting-watchdog@${ch}.timer"
    log "watchdog@${ch}.timer: enabled + started"
done

# ── 4. 旧 cron エントリを削除（移行済みのもののみ） ────────────
OLD_PATTERNS=(
    "ops/watchdog.sh ch1"
    "ops/watchdog.sh ch2"
    "ops/healthcheck-restart.sh"
)
CURRENT=$(crontab -l 2>/dev/null || true)
UPDATED="$CURRENT"
REMOVED=0
for pat in "${OLD_PATTERNS[@]}"; do
    if echo "$UPDATED" | grep -qF "$pat"; then
        UPDATED=$(echo "$UPDATED" | grep -vF "$pat")
        log "removed cron: $pat"
        REMOVED=$((REMOVED + 1))
    fi
done
if [[ $REMOVED -gt 0 ]]; then
    printf '%s\n' "$UPDATED" | grep -v '^$' | crontab -
    log "crontab updated ($REMOVED entries removed)"
fi

# ── 5. 状態確認 ────────────────────────────────────────────────
log ""
log "=== インストール完了 ==="
systemctl --user status aoi-broadcasting-monitor.service --no-pager | head -8
echo ""
systemctl --user list-timers 'aoi-broadcasting-*' --no-pager
