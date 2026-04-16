#!/usr/bin/env bash
# YouTube Data API v3 依存セットアップスクリプト (VPS用)
# 実行場所: /home/shugo/services/aoi-broadcasting/
# 実行: bash ops/setup_yt_api.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/yt-venv"
CREDS_DIR="$SCRIPT_DIR/credentials"

echo "=== YouTube Data API セットアップ ==="

# 1. venv 作成
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# 2. 依存インストール
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements_yt.txt"
echo "Dependencies installed."

# 3. credentials ディレクトリ
mkdir -p "$CREDS_DIR"
chmod 700 "$CREDS_DIR"

# 4. 確認
echo
echo "=== セットアップ完了 ==="
echo "Python: $("$VENV_DIR/bin/python3" --version)"
echo "venv: $VENV_DIR"
echo "credentials dir: $CREDS_DIR"
echo
if [ -f "$CREDS_DIR/client_secret.json" ]; then
    echo "✅ client_secret.json: 存在"
else
    echo "❌ client_secret.json: 未配置"
    echo "   Google Cloud Console でダウンロードして配置してください:"
    echo "   $CREDS_DIR/client_secret.json"
fi
if [ -f "$CREDS_DIR/token.json" ]; then
    echo "✅ token.json: 存在"
else
    echo "❌ token.json: 未生成"
    echo "   ローカルで認証後に転送してください:"
    echo "   python3 ops/yt_auth.py  (ローカルMacで実行)"
    echo "   scp ops/credentials/token.json contabo:/home/shugo/services/aoi-broadcasting/ops/credentials/"
fi

echo
echo "Next: ops/yt_go_live.py --dry-run でテスト"
