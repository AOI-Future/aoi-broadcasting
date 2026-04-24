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
TOKEN_FILES = {
    "CH1": CREDENTIALS_DIR / "token.json",
    "CH2": CREDENTIALS_DIR / "token_ch2.json",
}

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_credentials(channel: str = "CH1") -> Credentials:
    token_file = TOKEN_FILES.get(channel.upper(), TOKEN_FILES["CH1"])
    if not token_file.exists():
        alt = "token_ch2.json" if channel.upper() == "CH2" else "token.json"
        print(f"ERROR: {token_file} not found.")
        print(f"Run: python3 ops/yt_auth.py --headless --token-file ops/credentials/{alt}")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
        print("Token refreshed.")

    if not creds.valid:
        print(f"ERROR: Token is invalid. Run yt_auth.py --headless --token-file ops/credentials/{token_file.name}")
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

def end_broadcasts_for_stream(youtube, stream_id: str, dry_run: bool = False) -> None:
    """stream_id に bind されている active broadcast のみ終了させる。
    他チャンネルの broadcast は boundStreamId が異なるためスキップされる。"""
    resp = youtube.liveBroadcasts().list(
        part="id,snippet,status,contentDetails",
        broadcastStatus="active",
        maxResults=10,
    ).execute()
    for b in resp.get("items", []):
        bound = b.get("contentDetails", {}).get("boundStreamId", "")
        bid = b["id"]
        title = b["snippet"]["title"][:50]
        if bound != stream_id:
            print(f"Skipping broadcast {bid} ({title}) — bound to {bound or 'none'}")
            continue
        print(f"Ending broadcast for stream {stream_id}: {bid} ({title})")
        if not dry_run:
            try:
                youtube.liveBroadcasts().transition(
                    part="status",
                    id=bid,
                    broadcastStatus="complete",
                ).execute()
                print(f"  → Ended: {bid}")
            except Exception as e:
                print(f"  → WARNING: Could not end {bid}: {e}")


def get_latest_broadcast_meta(youtube) -> tuple[str, str, list[str]]:
    """直近の liveBroadcast からタイトル・説明文・タグを取得する（フォールバック用）。"""
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
                tags = snip.get("tags") or []
                return snip.get("title", ""), snip.get("description", ""), tags
        except Exception:
            pass
    return "", "", []


def read_env_broadcast_meta(env_file: Path) -> tuple[str, str, list[str]]:
    """
    .env ファイルから番組メタデータを読み込む。
    Returns: (title, description, tags)

    対応キー:
      YOUTUBE_BROADCAST_TITLE       番組タイトル
      YOUTUBE_BROADCAST_DESCRIPTION 説明文（\\n で改行）
      YOUTUBE_BROADCAST_TAGS        タグ（カンマ区切り）
    """
    title = description = ""
    tags: list[str] = []

    if not env_file.exists():
        return title, description, tags

    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "YOUTUBE_BROADCAST_TITLE":
            title = value
        elif key == "YOUTUBE_BROADCAST_DESCRIPTION":
            description = value.replace("\\n", "\n")
        elif key == "YOUTUBE_BROADCAST_TAGS":
            tags = [t.strip() for t in value.split(",") if t.strip()]

    return title, description, tags


def create_broadcast(
    youtube,
    title: str,
    description: str,
    tags: list[str],
    dry_run: bool = False,
    monetize: bool = True,
) -> dict:
    """
    liveBroadcast を作成する。
    enableAutoStart=True + enableMonitorStream=False により
    RTMP ストリームが active になった瞬間に自動で live 遷移する。
    monetize=True のとき収益化 On / Auto / High を設定する。
    """
    scheduled_start = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # YouTube タグ制約: 各タグ 30 文字以内、合計 500 文字以内、最大 30 個
    sanitized_tags = [t[:30] for t in tags[:30]]
    if sum(len(t) for t in sanitized_tags) > 500:
        sanitized_tags = sanitized_tags[:10]

    body = {
        "snippet": {
            "title": title,
            "scheduledStartTime": scheduled_start,
            "description": description,
            "tags": sanitized_tags,
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

    parts = "id,snippet,status,contentDetails"

    if monetize:
        body["monetizationDetails"] = {
            "adsMonetizationStatus": "on",
            "cuepointSchedule": {
                "enabled": True,
                "ytOptimizedCuepointConfig": "HIGH",  # 広告挿入頻度: 高
            },
        }
        parts += ",monetizationDetails"
        print("Monetization: On / Auto / High")
    else:
        print("Monetization: disabled (--no-monetize)")

    if dry_run:
        print(f"[DRY RUN] Would create broadcast: {title}")
        print(f"[DRY RUN] Tags: {sanitized_tags}")
        print(f"[DRY RUN] Monetize: {monetize}")
        return {"id": "DRY_RUN_BROADCAST_ID", "snippet": {"title": title}}

    print(f"Creating broadcast: {title}")
    print(f"  Tags ({len(sanitized_tags)}): {sanitized_tags}")
    response = youtube.liveBroadcasts().insert(
        part=parts,
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
    parser.add_argument("--no-monetize", action="store_true",
                        help="収益化設定をスキップ (YPP非対応チャンネル用)")
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

    # 認証（チャンネルに対応したトークンファイルを使用）
    creds = load_credentials(channel=args.channel)
    youtube = build("youtube", "v3", credentials=creds)

    # liveStream 検索（stream_id 確定後に終了処理を行うため先に実行）
    stream = find_stream_by_key(youtube, stream_key)
    if not stream:
        print("ERROR: liveStream not found. Verify YOUTUBE_KEY is correct.")
        sys.exit(2)
    stream_id = stream["id"]

    # 自チャンネルの stream に bind された broadcast のみ終了（他チャンネルのは触らない）
    end_broadcasts_for_stream(youtube, stream_id, dry_run=args.dry_run)

    # ─── メタデータ決定 (優先順位: .env > 直前のbroadcast > デフォルト) ───
    now_jst = datetime.now(JST)
    default_title = f"NICTIA Radio {now_jst.strftime('%Y-%m-%d')} {args.channel}"
    default_desc = "NICTIA Radio — AI-powered ambient music station by AOI Future"

    env_title, env_desc, env_tags = ("", "", []) if args.dry_run else read_env_broadcast_meta(env_file)

    if env_title and env_desc and env_tags:
        # .env にすべて揃っている → そのまま使用
        use_title, use_desc, use_tags = env_title, env_desc, env_tags
        print(f"Metadata from .env: title={use_title[:60]!r}  tags={use_tags}")
    else:
        # 直前の broadcast から引き継ぎ（.env に不足分がある場合のフォールバック）
        prev_title, prev_desc, prev_tags = ("", "", []) if args.dry_run else get_latest_broadcast_meta(youtube)
        use_title = env_title or prev_title or default_title
        use_desc  = env_desc  or prev_desc  or default_desc
        use_tags  = env_tags  or prev_tags  or []
        source = ".env(partial)+API" if (env_title or env_desc or env_tags) else ("API" if prev_title else "default")
        print(f"Metadata source: {source}  title={use_title[:60]!r}  tags={use_tags}")

    # Broadcast 作成
    try:
        broadcast = create_broadcast(
            youtube, use_title, use_desc, use_tags,
            dry_run=args.dry_run,
            monetize=not args.no_monetize,
        )
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
