# aoi-broadcasting

Headless streaming server — static background image + WAV playlist to YouTube/Kick via ffmpeg.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/AOI-Future/aoi-broadcasting.git
cd aoi-broadcasting

# 2. Configure
cp .env.example .env
# Edit .env with your stream keys

# 3. Add media
# Place background image (jpg/png) in assets/
# Place WAV files in music/

# 4. Run
docker compose up -d
docker logs -f aoi-broadcasting
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `YOUTUBE_URL` | At least one pair | RTMP ingest URL for YouTube |
| `YOUTUBE_KEY` | At least one pair | YouTube stream key |
| `KICK_URL` | At least one pair | RTMP ingest URL for Kick |
| `KICK_KEY` | At least one pair | Kick stream key |
| `ARCHIVE_DAYS` | Optional | Days before music files are moved to `archive/` (default: 30) |
| `ARCHIVE_RETENTION_DAYS` | Optional | Days before archived files are deleted (default: 0 = disabled) |
| `WAIT_NO_MUSIC` | Optional | Seconds to wait before rechecking when no music is found (default: 30) |
| `RESTART_DELAY` | Optional | Base seconds before restarting after ffmpeg exits (default: 5) |
| `MAX_RESTART_DELAY` | Optional | Max seconds for exponential backoff after ffmpeg failures (default: 60) |
| `NORMALIZE_MAX_FILES_PER_CYCLE` | Optional | 配信中のバックグラウンド正規化で1回あたり処理する最大件数 (default: 8) |
| `NORMALIZE_BOOTSTRAP_BATCH` | Optional | 起動/再起動直後にまとめて先行正規化する件数 (default: 50) |
| `NORMALIZE_DURING_STREAM_INTERVAL` | Optional | 配信中に未処理音源を正規化する間隔（秒）(default: 120) |
| `NORMALIZE_TARGET_I` | Optional | loudnormの目標Integrated Loudness (LUFS, default: -16) |
| `NORMALIZE_TARGET_LRA` | Optional | loudnormの目標Loudness Range (default: 11) |
| `NORMALIZE_TARGET_TP` | Optional | loudnormの目標True Peak (dBTP, default: -1.5) |
| `NORMALIZE_NICE_LEVEL` | Optional | 正規化ffmpegプロセスのnice値。大きいほど低優先度 (default: 10) |
| `FFMPEG_NORMALIZE_TIMEOUT` | Optional | 1ファイル正規化ffmpegのタイムアウト秒 (default: 1800) |
| `FFMPEG_LOOP_PREENCODE_TIMEOUT` | Optional | 背景ループ動画の事前エンコードタイムアウト秒 (default: 120) |
| `LOG_LEVEL` | Optional | Logging level (default: INFO) |
| `STREAM_KEY` format | Note | Keys with leading/trailing spaces, control characters, or `|[]` are rejected for safety |
| `music/` filename safety | Note | 制御文字を含む危険なファイル名はスキップされます |

At least one destination (YouTube or Kick) must be configured.

## Security Hardening

デフォルトの `docker-compose.yml` は長期運用向けに以下の設定を有効化しています。

- **非rootユーザーで実行**（UID/GID: `10001`）
- **`read_only: true`** によるルートFSの書き込み禁止
- **`no-new-privileges` と `cap_drop: ALL`** による権限削減
- **`/tmp` を tmpfs でマウント**（一時ファイル専用）

ホスト側の `music/` `archive/` `normalized/` ディレクトリは UID/GID `10001` が書き込める権限にしてください。

## Architecture

```
┌─────────────────────────────────────────┐
│  aoi-broadcasting container             │
│                                         │
│  main.py (Python)                       │
│    ├─ Collects WAV files from /data/music│
│    ├─ Normalizes WAV in background      │
│    ├─ Builds ffmpeg concat playlist     │
│    ├─ Pre-encodes background → loop.flv │
│    └─ Streams via ffmpeg tee muxer      │
│         ├─ → YouTube RTMP               │
│         └─ → Kick RTMP                  │
│                                         │
│  Volumes:                               │
│    ./music   → /data/music   (WAV files)│
│    ./archive    → /data/archive (auto-aged)│
│    ./normalized → /data/normalized (cached loudness)│
│    ./assets     → /app/assets   (bg image) │
└─────────────────────────────────────────┘
```

### Streaming Cycle

1. **Idle Maintenance** — 古い音源を `archive/` へ移動し、不要な正規化キャッシュを掃除
2. **Bootstrap Normalize** — 配信開始前に未処理WAVを先行正規化（件数上限あり）
3. **Collect** — 正規化済みWAVのみでプレイリストを構築（音量の一貫性を保証）
4. **Stream** — ffmpeg streams the normalized playlist with background image
5. **During Stream** — 配信を止めずに低優先度で未処理音源をバックグラウンド正規化
6. **Loop** — when playlist ends, cycle restarts with fresh shuffle


### Loudness Stabilization Strategy (CPU最適化)

- 配信経路ではリアルタイム正規化を行わず、**事前正規化済みWAVのみを配信**します。
- 新規音源は配信中でもバックグラウンドで低優先度（nice）処理し、配信停止を回避します。
- `NORMALIZE_BOOTSTRAP_BATCH` と `NORMALIZE_MAX_FILES_PER_CYCLE` で、初期処理と定常処理の負荷を分離して制御できます。
- 元ファイルが更新された場合はシグネチャが変わるため自動で再生成され、不要キャッシュは自動削除されます。

## Directory Structure

```
aoi-broadcasting/
├── README.md
├── .gitignore
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── main.py
├── assets/          # Place background.jpg here
│   └── .gitkeep
├── music/           # Place WAV files here
│   └── .gitkeep
└── normalized/      # Auto-generated loudness-normalized cache
    └── .gitkeep
```

## Release & Security Operations

公開運用に向けた最低限の運用は以下です。

- CI (`.github/workflows/ci.yml`) で `py_compile` + `unittest` を必須化
- CodeQL (`.github/workflows/codeql.yml`) による静的解析をPR/定期実行
- Dependabot (`.github/dependabot.yml`) で Docker / GitHub Actions 更新を週次追従
- 脆弱性対応手順は `SECURITY.md` を参照

## Troubleshooting

### No tracks found
```
No tracks found in /data/music. Waiting 30s...
```
Place WAV files in `music/` directory. The streamer will detect them on the next cycle.

### No stream destinations configured
```
No stream destinations configured. Set YOUTUBE_URL/KEY or KICK_URL/KEY.
```
Check `.env` file — at least one YouTube or Kick destination is required.
`|` や `[]` を含む stream key、先頭/末尾スペース付き key は拒否されます。

### Background image not found
```
Background image not found: /app/assets/background.jpg
```
Place a JPG or PNG image in `assets/` directory.

### ffmpeg exits immediately
Check stream keys are valid and the ingest server is reachable. The container will automatically retry after 5 seconds.

## Monitoring (Prometheus)

このリポジトリは Prometheus 監視の実装を内蔵していないため、必要に応じて運用環境側で以下の前提条件と設定を用意してください。
Prometheus を導入しない場合は、`docker logs -f aoi-broadcasting` による基本監視でも運用可能です。

### 前提条件

- **Prometheus サーバが稼働していること**（v2.x 系を想定、任意）
- **監視対象ホストにエクスポータが導入されていること**
  - 推奨: `node_exporter`（CPU/メモリ/ディスク）
  - 推奨: `cadvisor`（コンテナのCPU/メモリ/ネットワーク）
- **Docker のメトリクス取得が許可されていること**
  - `cadvisor` か Docker Engine API へのアクセス権

### 必要な設定（例）

1. **node_exporter**
   - ポート: `9100`
   - 例: `docker run -d --name node_exporter -p 9100:9100 prom/node-exporter`
2. **cadvisor**
   - ポート: `8080`
   - 例: `docker run -d --name cadvisor -p 8080:8080 --privileged gcr.io/cadvisor/cadvisor:latest`
3. **Prometheus の scrape 設定**
   ```yaml
   scrape_configs:
     - job_name: "node_exporter"
       static_configs:
         - targets: ["<server-ip>:9100"]
     - job_name: "cadvisor"
       static_configs:
         - targets: ["<server-ip>:8080"]
   ```

### 推奨アラート（例）

- **ディスク使用率が高い**（アーカイブの増加を検知）
- **コンテナ再起動が頻発**（ffmpeg の失敗を検知）
- **配信先へのネットワーク不通**（RTMP 接続失敗率）

上記は運用環境の構成に合わせて調整してください。

## License

Private — AOI-Future
