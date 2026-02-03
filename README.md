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

At least one destination (YouTube or Kick) must be configured.

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

## License

Private — AOI-Future
