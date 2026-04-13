#!/usr/bin/env bash
# setup-cron.sh — aoi-broadcasting の cron エントリを冪等に設定する
# Usage: ./ops/setup-cron.sh
# 何度実行しても安全。不足エントリのみ追加し、既存は変更しない。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$SCRIPT_DIR/watchdog.sh"
HEALTHCHECK="$SCRIPT_DIR/healthcheck-restart.sh"
LOG_DIR="$SCRIPT_DIR"

ENTRIES=(
  "*/5 * * * * $WATCHDOG ch1 >> $LOG_DIR/watchdog-ch1.log 2>&1"
  "*/5 * * * * $WATCHDOG ch2 >> $LOG_DIR/watchdog-ch2.log 2>&1"
  "*/3 * * * * $HEALTHCHECK >> $LOG_DIR/healthcheck-restart.log 2>&1"
)

CURRENT=$(crontab -l 2>/dev/null || true)
UPDATED="$CURRENT"
ADDED=0

for entry in "${ENTRIES[@]}"; do
  if echo "$CURRENT" | grep -qF "$entry"; then
    echo "[SKIP] already present: $entry"
  else
    UPDATED=$(printf '%s\n%s\n' "$UPDATED" "$entry")
    echo "[ADD]  $entry"
    ADDED=$((ADDED + 1))
  fi
done

if [ $ADDED -eq 0 ]; then
  echo "All cron entries already configured. Nothing to do."
else
  echo "$UPDATED" | grep -v '^$' | crontab -
  echo "Done: added $ADDED entr$([ $ADDED -eq 1 ] && echo y || echo ies)."
fi
