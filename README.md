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
| `ARCHIVE_RETENTION_DAYS` | Optional | Days before archived files are deleted (default: 90) |
| `WAIT_NO_MUSIC` | Optional | Seconds to wait before rechecking when no music is found (default: 30) |
| `RESTART_DELAY` | Optional | Base seconds before restarting after ffmpeg exits (default: 5) |
| `MAX_RESTART_DELAY` | Optional | Max seconds for exponential backoff after ffmpeg failures (default: 60) |
| `LOG_LEVEL` | Optional | Logging level (default: INFO) |

At least one destination (YouTube or Kick) must be configured.

## Security Hardening

デフォルトの `docker-compose.yml` は長期運用向けに以下の設定を有効化しています。

- **非rootユーザーで実行**（UID/GID: `10001`）
- **`read_only: true`** によるルートFSの書き込み禁止
- **`no-new-privileges` と `cap_drop: ALL`** による権限削減
- **`/tmp` を tmpfs でマウント**（一時ファイル専用）

ホスト側の `music/` と `archive/` ディレクトリは UID/GID `10001` が書き込める権限にしてください。

## Architecture

```
┌─────────────────────────────────────────┐
│  aoi-broadcasting container             │
│                                         │
│  main.py (Python)                       │
│    ├─ Collects WAV files from /data/music│
│    ├─ Builds ffmpeg concat playlist     │
│    ├─ Pre-encodes background → loop.flv │
│    └─ Streams via ffmpeg tee muxer      │
│         ├─ → YouTube RTMP               │
│         └─ → Kick RTMP                  │
│                                         │
│  Volumes:                               │
│    ./music   → /data/music   (WAV files)│
│    ./archive → /data/archive (auto-aged)│
│    ./assets  → /app/assets   (bg image) │
└─────────────────────────────────────────┘
```

### Streaming Cycle

1. **Maintenance** — files older than 30 days are moved to `archive/`
2. **Collect** — all WAV files in `music/` are shuffled into a playlist
3. **Stream** — ffmpeg streams the playlist with background image
4. **Loop** — when playlist ends, cycle restarts with fresh shuffle

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
└── music/           # Place WAV files here
    └── .gitkeep
```

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

### Background image not found
```
Background image not found: /app/assets/background.jpg
```
Place a JPG or PNG image in `assets/` directory.

### ffmpeg exits immediately
Check stream keys are valid and the ingest server is reachable. The container will automatically retry after 5 seconds.

## Monitoring (Prometheus)

このリポジトリは Prometheus 監視の実装を内蔵していないため、運用環境側で以下の前提条件と設定を用意してください。

### 前提条件

- **Prometheus サーバが稼働していること**（v2.x 系を想定）
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
