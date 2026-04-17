#!/usr/bin/env python3
"""
One-time OAuth2 authentication for YouTube Data API v3.

Run this ONCE on any machine (local Mac is easiest), then transfer token.json to VPS:
    scp ops/credentials/token.json contabo:/home/shugo/services/aoi-broadcasting/ops/credentials/

Prerequisites:
    pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client

Setup:
    1. Google Cloud Console → YouTube Data API v3 を有効化
    2. OAuth 2.0 クライアント ID 作成（種類: デスクトップアプリ）
    3. client_secret.json を ops/credentials/ に配置
    4. python3 yt_auth.py を実行
"""

import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CREDENTIALS_DIR = Path(__file__).parent / "credentials"
CLIENT_SECRET_FILE = CREDENTIALS_DIR / "client_secret.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"


def authenticate(token_file: Path = TOKEN_FILE, headless: bool = False) -> Credentials:
    creds = None

    # 既存トークンを読み込む
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    # 有効期限切れなら更新
    if creds and creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
        _save_token(creds, token_file)
        print("Token refreshed successfully.")
        return creds

    if creds and creds.valid:
        print("Token is valid, no re-authentication needed.")
        return creds

    # 新規認証
    if not CLIENT_SECRET_FILE.exists():
        print(f"ERROR: {CLIENT_SECRET_FILE} not found.")
        print()
        print("Setup steps:")
        print("  1. https://console.cloud.google.com/ でプロジェクト作成")
        print("  2. 「APIとサービス」→「ライブラリ」→ YouTube Data API v3 を有効化")
        print("  3. 「認証情報」→「OAuth 2.0 クライアント ID」作成 (種類: デスクトップ アプリ)")
        print("  4. JSON をダウンロードして ops/credentials/client_secret.json として保存")
        raise FileNotFoundError(str(CLIENT_SECRET_FILE))

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
    )

    # ブラウザが使える環境: run_local_server() でリダイレクトを自動キャッチ
    # ブラウザなし (VPS): --headless フラグで手動コード入力
    if headless:
        flow.redirect_uri = "http://localhost"
        print("Headless mode: Visit the URL below and paste the authorization code.")
        print()
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print(f"Open this URL:\n  {auth_url}\n")
        print("※ 複数チャンネルある場合: ブラウザで認証後、チャンネル選択画面が出たら対象チャンネルに切り替えてください")
        print("※ 認証後にブラウザが localhost に繋がらないエラーになりますが正常です")
        print("  → アドレスバーの URL から code= の値をコピーしてください")
        print()
        code = input("Enter authorization code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        print("Opening browser for OAuth authorization...")
        creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds, token_file)
    print(f"\nToken saved to: {token_file}")
    return creds


def _save_token(creds: Credentials, token_file: Path = TOKEN_FILE) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())


if __name__ == "__main__":
    import argparse as _argparse
    parser = _argparse.ArgumentParser(description="YouTube Data API OAuth2 認証")
    parser.add_argument("--headless", action="store_true",
                        help="ブラウザなし環境（VPS）で手動コード入力モード")
    parser.add_argument("--token-file", default=str(TOKEN_FILE),
                        help=f"保存先トークンファイル (default: {TOKEN_FILE})\n"
                             "CH2用: ops/credentials/token_ch2.json")
    args = parser.parse_args()

    token_path = Path(args.token_file)
    creds = authenticate(token_file=token_path, headless=args.headless)
    print()
    print("Authentication successful!")
    print(f"Token valid: {creds.valid}")
    if creds.expiry:
        print(f"Token expiry: {creds.expiry} UTC")
        print()
        print("NOTE: Testing mode tokens expire in 7 days.")
        print("      Production mode (OAuth consent screen verification) → indefinite.")
    print()
    print("Next: transfer token file to VPS if needed:")
    print(f"  scp {token_path} contabo:/home/shugo/services/aoi-broadcasting/ops/credentials/")
