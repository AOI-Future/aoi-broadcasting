"""Headless streaming server - static image + WAV playlist to YouTube/Kick."""

import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("streamer")

MUSIC_DIR = Path("/data/music")
ARCHIVE_DIR = Path("/data/archive")
ASSETS_DIR = Path("/app/assets")
# Prefer JPG over PNG for faster decoding
BACKGROUND = next(
    (p for ext in ("*.jpg", "*.jpeg", "*.png") for p in ASSETS_DIR.glob(ext)),
    ASSETS_DIR / "background.jpg",
)

YOUTUBE_URL = os.environ.get("YOUTUBE_URL", "")
YOUTUBE_KEY = os.environ.get("YOUTUBE_KEY", "")
KICK_URL = os.environ.get("KICK_URL", "")
KICK_KEY = os.environ.get("KICK_KEY", "")

def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("Invalid %s=%d, using minimum %d", name, value, minimum)
        return minimum
    return value


ARCHIVE_DAYS = _env_int("ARCHIVE_DAYS", 30, minimum=1)
ARCHIVE_RETENTION_DAYS = _env_int("ARCHIVE_RETENTION_DAYS", 0, minimum=0)
WAIT_NO_MUSIC = _env_int("WAIT_NO_MUSIC", 30, minimum=5)  # seconds to wait when no music found
RESTART_DELAY = _env_int("RESTART_DELAY", 5, minimum=1)   # seconds before restarting after ffmpeg exits
MAX_RESTART_DELAY = _env_int("MAX_RESTART_DELAY", 60, minimum=5)


def _archive_destination(source: Path) -> Path:
    """Return a non-colliding destination path in ARCHIVE_DIR."""
    dest = ARCHIVE_DIR / source.name
    if not dest.exists():
        return dest
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return ARCHIVE_DIR / f"{source.stem}_{timestamp}{source.suffix}"


def archive_old_files() -> int:
    """Move files older than ARCHIVE_DAYS to archive directory."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now() - timedelta(days=ARCHIVE_DAYS)
    moved = 0

    for f in MUSIC_DIR.glob("*.wav"):
        if not f.is_file() or f.is_symlink():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            dest = _archive_destination(f)
            shutil.move(str(f), str(dest))
            log.info("Archived: %s -> %s (mtime: %s)", f.name, dest.name, mtime.isoformat())
            moved += 1

    if moved:
        log.info("Archived %d file(s)", moved)
    return moved


def prune_archive() -> int:
    """Delete archive files older than ARCHIVE_RETENTION_DAYS."""
    if ARCHIVE_RETENTION_DAYS <= 0:
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    removed = 0
    for f in ARCHIVE_DIR.glob("*.wav"):
        if not f.is_file() or f.is_symlink():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        log.info("Pruned %d archived file(s)", removed)
    return removed


def collect_tracks() -> list[Path]:
    """Get all WAV files in music directory, shuffled."""
    tracks = []
    for track in MUSIC_DIR.glob("*.wav"):
        if not track.is_file() or track.is_symlink():
            continue
        tracks.append(track)
    tracks.sort()
    random.shuffle(tracks)
    return tracks


def build_playlist(tracks: list[Path], tmpdir: str) -> Path:
    """Create ffmpeg concat demuxer playlist file."""
    playlist = Path(tmpdir) / "playlist.txt"
    with open(playlist, "w", encoding="utf-8") as f:
        for track in tracks:
            # Escape single quotes in filename for ffmpeg concat
            safe = str(track).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    log.info("Playlist: %d tracks", len(tracks))
    return playlist


def _valid_rtmp_target(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"rtmp", "rtmps"}:
        return False
    return bool(parsed.netloc)


def build_outputs() -> str:
    """Build tee muxer output string for configured destinations."""
    outputs = []

    if YOUTUBE_URL and YOUTUBE_KEY:
        yt = f"{YOUTUBE_URL}/{YOUTUBE_KEY}"
        if _valid_rtmp_target(yt):
            outputs.append(f"[f=flv]{yt}")
            log.info("YouTube output configured")
        else:
            log.warning("Invalid YouTube URL; expected rtmp/rtmps scheme")

    if KICK_URL and KICK_KEY:
        kick = f"{KICK_URL}/{KICK_KEY}"
        if _valid_rtmp_target(kick):
            outputs.append(f"[f=flv]{kick}")
            log.info("Kick output configured")
        else:
            log.warning("Invalid Kick URL; expected rtmp/rtmps scheme")

    if not outputs:
        log.error("No stream destinations configured. Set YOUTUBE_URL/KEY or KICK_URL/KEY.")
        sys.exit(1)

    return "|".join(outputs)


LOOP_VIDEO = Path("/tmp/loop.flv")


def _ensure_loop_video():
    """Pre-encode background into a 10-min FLV loop at 5fps for -stream_loop."""
    if not BACKGROUND.exists():
        log.error("Background image not found: %s", BACKGROUND)
        sys.exit(1)
    if LOOP_VIDEO.exists():
        if LOOP_VIDEO.stat().st_mtime >= BACKGROUND.stat().st_mtime:
            return
        log.info("Background updated; regenerating loop video")
    log.info("Pre-encoding loop video from %s ...", BACKGROUND.name)
    tmp = Path(tempfile.mkstemp(suffix=".flv")[1])
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(BACKGROUND),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-preset", "ultrafast",
            "-b:v", "500k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=1280:720",
            "-r", "5",
            "-g", "10",
            "-t", "10",
            "-an",
            "-f", "flv",
            str(tmp),
        ],
        check=True,
        capture_output=True,
    )
    tmp.replace(LOOP_VIDEO)
    log.info("Loop video ready: %s (%.1f MB)", LOOP_VIDEO, LOOP_VIDEO.stat().st_size / 1e6)


def run_ffmpeg(playlist: Path, output_tee: str) -> int:
    """Run ffmpeg streaming process. Returns exit code."""
    _ensure_loop_video()

    cmd = [
        "ffmpeg",
        "-re",
        # Input 1: pre-encoded FLV loop (infinite)
        "-stream_loop", "-1",
        "-i", str(LOOP_VIDEO),
        # Input 2: audio playlist
        "-f", "concat",
        "-safe", "0",
        "-i", str(playlist),
        # Video: copy (already H.264 in FLV)
        "-c:v", "copy",
        # Audio encoding
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        # End when audio finishes
        "-shortest",
        # Output via tee muxer
        "-f", "tee",
        "-map", "0:v",
        "-map", "1:a",
        output_tee,
    ]

    log.info("Starting ffmpeg stream...")
    try:
        proc = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
        return proc.returncode
    except KeyboardInterrupt:
        log.info("Stream interrupted by user")
        raise
    except Exception as e:
        log.error("ffmpeg error: %s", e)
        return 1


def main():
    log.info("=== Headless Streamer starting ===")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    output_tee = build_outputs()

    restart_delay = RESTART_DELAY
    while True:
        try:
            # Phase 1: Maintenance
            log.info("--- Maintenance phase ---")
            archive_old_files()
            prune_archive()

            # Phase 2: Collect and check tracks
            tracks = collect_tracks()
            if not tracks:
                log.warning("No tracks found in %s. Waiting %ds...", MUSIC_DIR, WAIT_NO_MUSIC)
                time.sleep(WAIT_NO_MUSIC)
                continue

            # Phase 3: Build playlist and stream
            with tempfile.TemporaryDirectory() as tmpdir:
                playlist = build_playlist(tracks, tmpdir)
                rc = run_ffmpeg(playlist, output_tee)
                log.info("ffmpeg exited with code %d", rc)
                if rc != 0:
                    restart_delay = min(restart_delay * 2, MAX_RESTART_DELAY)
                else:
                    restart_delay = RESTART_DELAY

            # Phase 4: Brief pause before next cycle
            log.info("Restarting cycle in %ds...", restart_delay)
            time.sleep(restart_delay)

        except KeyboardInterrupt:
            log.info("Shutting down gracefully")
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            log.info("Retrying in %ds...", RESTART_DELAY)
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
