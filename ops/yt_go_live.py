#!/usr/bin/env python3
"""
YouTube Data API v3 — liveBroadcast 自動作成・Go Live スクリプト
aoi-broadcasting CH1/CH2 用

Usage:
    python3 yt_go_live.py [--channel CH1] [--dry-run] [--wait-stream]

Exit codes:
    0  成功 (BROADCAST_ID と WATCH_URL を stdout 末尾に出力)
    1  認証エラー / API エラー
    2  liveStream が見つからない

watchdog.sh からの呼び出し例:
    output=$(python3 /home/shugo/services/aoi-broadcasting/ops/yt_go_live.py --channel CH1)
    watch_url=$(echo "$output" | grep '^WATCH_URL=' | cut -d= -f2-)
"""

import os
import sys
import re
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CREDENTIALS_DIR = Path(__file__).parent / "credentials"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} not found. Run yt_auth.py first.")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        print("Token refreshed.")

    if not creds.valid:
        print("ERROR: Token is invalid. Run yt_auth.py to re-authenticate.")
        sys.exit(1)

    return creds


# ---------------------------------------------------------------------------
# liveStreams
# ---------------------------------------------------------------------------

def find_stream_by_key(youtube, stream_key: str) -> dict | None:
    """stream_key に一致する liveStream を返す（永続ストリームエンドポイント）。"""
    print(f"Looking up liveStream for key: {stream_key[:8]}****...")
    response = youtube.liveStreams().list(
        part="id,snippet,cdn,status",
        mine=True,
        maxResults=50,
    ).execute()

    for item in response.get("items", []):
        ingestion_key = item.get("cdn", {}).get("ingestionInfo", {}).get("streamName", "")
        if ingestion_key == stream_key:
            stream_id = item["id"]
            status = item.get("status", {}).get("streamStatus", "unknown")
            print(f"Found liveStream: {stream_id} (streamStatus: {status})")
            return item

    print("WARNING: liveStream not found by key. Available streams:")
    for item in response.get("items", []):
        print(f"  id={item['id']}  title={item['snippet']['title']}")
    return None


def wait_for_stream_active(youtube, stream_id: str, max_wait: int = 300) -> bool:
    """RTMP データが流れ始めるまで最大 max_wait 秒ポーリング。"""
    print(f"Waiting for stream {stream_id} to become active (max {max_wait}s)...")
    interval = 15
    elapsed = 0

    while elapsed < max_wait:
        resp = youtube.liveStreams().list(part="status", id=stream_id).execute()
        items = resp.get("items", [])
        if items:
            status = items[0].get("status", {}).get("streamStatus", "unknown")
            print(f"  streamStatus: {status} ({elapsed}s elapsed)")
            if status == "active":
                return True
        time.sleep(interval)
        elapsed += interval

    print(f"Stream did not become active within {max_wait}s.")
    return False


# ---------------------------------------------------------------------------
# liveBroadcasts
# ---------------------------------------------------------------------------

def get_latest_broadcast_meta(youtube) -> tuple[str, str]:
    """直近の liveBroadcast からタイトルと説明文を取得する（新規作成時に引き継ぐ）。"""
    for status in ("active", "completed"):
        try:
            resp = youtube.liveBroadcasts().list(
                part="snippet",
                broadcastStatus=status,
                maxResults=1,
            ).execute()
            items = resp.get("items", [])
            if items:
                snip = items[0]["snippet"]
                return snip.get("title", ""), snip.get("description", "")
        except Exception:
            pass
    return "", ""


def create_broadcast(youtube, title: str, dry_run: bool = False) -> dict:
    """
    liveBroadcast を作成する。
    enableAutoStart=True + enableMonitorStream=False により
    RTMP ストリームが active になった瞬間に自動で live 遷移する。
    タイトル・説明文は直近のbroadcastから引き継ぐ。
    """
    scheduled_start = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # 直近broadcastのメタデータを引き継ぐ
    prev_title, prev_desc = ("", "") if dry_run else get_latest_broadcast_meta(youtube)
    use_title = prev_title if prev_title else title
    use_desc = prev_desc if prev_desc else "NICTIA Radio — AI-powered ambient music station by AOI Future"
    if prev_title:
        print(f"Inheriting title from previous broadcast: {use_title[:60]}...")
    if prev_desc:
        print(f"Inheriting description ({len(use_desc)} chars)")

    body = {
        "snippet": {
            "title": use_title,
            "scheduledStartTime": scheduled_start,
            "description": use_desc,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": True,       # RTMP active → 自動 live 遷移
            "enableAutoStop": False,       # 手動停止まで継続
            "enableMonitorStream": False,  # テストフェーズをスキップ
            "recordFromStart": True,
            "enableDvr": True,
            "latencyPreference": "low",
        },
    }

    if dry_run:
        print(f"[DRY RUN] Would create broadcast: {title}")
        return {"id": "DRY_RUN_BROADCAST_ID", "snippet": {"title": title}}

    print(f"Creating broadcast: {title}")
    response = youtube.liveBroadcasts().insert(
        part="id,snippet,status,contentDetails",
        body=body,
    ).execute()

    broadcast_id = response["id"]
    print(f"Broadcast created: {broadcast_id}")
    return response


def bind_broadcast(youtube, broadcast_id: str, stream_id: str, dry_run: bool = False) -> None:
    """liveBroadcast を liveStream に結びつける。"""
    if dry_run:
        print(f"[DRY RUN] Would bind {broadcast_id} → {stream_id}")
        return

    print(f"Binding broadcast {broadcast_id} → stream {stream_id}...")
    youtube.liveBroadcasts().bind(
        part="id,contentDetails",
        id=broadcast_id,
        streamId=stream_id,
    ).execute()
    print("Bind successful.")


# ---------------------------------------------------------------------------
# .env update
# ---------------------------------------------------------------------------

def update_env_file(env_file: Path, broadcast_id: str, dry_run: bool = False) -> str:
    """YOUTUBE_WATCH_URL を .env ファイルに書き込む。"""
    watch_url = f"https://youtube.com/live/{broadcast_id}"

    if dry_run:
        print(f"[DRY RUN] Would write to {env_file}:")
        print(f"  YOUTUBE_WATCH_URL={watch_url}")
        return watch_url

    if not env_file.exists():
        print(f"WARNING: {env_file} not found, skipping env update.")
        return watch_url

    content = env_file.read_text()
    new_content = re.sub(
        r"^(YOUTUBE_WATCH_URL=).*$",
        rf"\g<1>{watch_url}",
        content,
        flags=re.MULTILINE,
    )
    if new_content == content:
        # キーが存在しない場合は末尾に追加
        new_content = content.rstrip("\n") + f"\nYOUTUBE_WATCH_URL={watch_url}\n"

    env_file.write_text(new_content)
    print(f"Updated {env_file.name}: YOUTUBE_WATCH_URL={watch_url}")
    return watch_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube liveBroadcast 自動作成・Go Live")
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"],
                        help="対象チャンネル (default: CH1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="API 呼び出しなしでシミュレート")
    parser.add_argument("--wait-stream", action="store_true",
                        help="stream が active になるまで待機してから終了")
    parser.add_argument("--env-file",
                        help=".env ファイルのパス (省略時は ../.env.ch{n})")
    args = parser.parse_args()

    ch_num = args.channel.lower().replace("ch", "")
    env_file = Path(args.env_file) if args.env_file else (
        Path(__file__).parent.parent / f".env.ch{ch_num}"
    )

    # stream_key を環境変数 or .env ファイルから取得
    stream_key = os.environ.get("YOUTUBE_KEY")
    if not stream_key and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("YOUTUBE_KEY="):
                stream_key = line.split("=", 1)[1].strip()
                break

    if not stream_key:
        print("ERROR: YOUTUBE_KEY not found in environment or env file.")
        sys.exit(2)

    print(f"=== yt_go_live.py: {args.channel} ===")
    if args.dry_run:
        print("[DRY RUN MODE — no API calls]")

    # 認証
    creds = load_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    # liveStream 検索
    stream = find_stream_by_key(youtube, stream_key)
    if not stream:
        print("ERROR: liveStream not found. Verify YOUTUBE_KEY is correct.")
        sys.exit(2)
    stream_id = stream["id"]

    # タイトル生成
    now_jst = datetime.now(JST)
    title = f"NICTIA Radio {now_jst.strftime('%Y-%m-%d')} {args.channel}"

    # Broadcast 作成
    try:
        broadcast = create_broadcast(youtube, title, dry_run=args.dry_run)
    except HttpError as e:
        print(f"ERROR creating broadcast: {e}")
        sys.exit(1)
    broadcast_id = broadcast["id"]

    # Bind
    try:
        bind_broadcast(youtube, broadcast_id, stream_id, dry_run=args.dry_run)
    except HttpError as e:
        print(f"ERROR binding broadcast: {e}")
        sys.exit(1)

    # stream active 待機（オプション）
    if args.wait_stream and not args.dry_run:
        active = wait_for_stream_active(youtube, stream_id)
        if not active:
            print("WARNING: Stream not yet active. Broadcast created with enableAutoStart=True.")
            print("         It will go live automatically once RTMP stream becomes active.")

    # .env 更新
    watch_url = update_env_file(env_file, broadcast_id, dry_run=args.dry_run)

    print()
    print("=== Result ===")
    print(f"Broadcast ID: {broadcast_id}")
    print(f"Watch URL:    {watch_url}")
    print()
    # watchdog.sh がパース用に読む機械可読行
    print(f"BROADCAST_ID={broadcast_id}")
    print(f"WATCH_URL={watch_url}")


if __name__ == "__main__":
    main()
