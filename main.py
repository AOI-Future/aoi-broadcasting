"""Headless streaming server - static image + WAV playlist to YouTube/Kick."""

import hashlib
import logging
import os
import re
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
NORMALIZED_DIR = Path("/data/normalized")

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
WAIT_NO_MUSIC = _env_int("WAIT_NO_MUSIC", 30, minimum=5)
RESTART_DELAY = _env_int("RESTART_DELAY", 5, minimum=1)
MAX_RESTART_DELAY = _env_int("MAX_RESTART_DELAY", 60, minimum=5)
NORMALIZE_MAX_FILES_PER_CYCLE = _env_int("NORMALIZE_MAX_FILES_PER_CYCLE", 8, minimum=1)
NORMALIZE_BOOTSTRAP_BATCH = _env_int("NORMALIZE_BOOTSTRAP_BATCH", 50, minimum=1)
NORMALIZE_DURING_STREAM_INTERVAL = _env_int("NORMALIZE_DURING_STREAM_INTERVAL", 120, minimum=30)
NORMALIZE_NICE_LEVEL = _env_int("NORMALIZE_NICE_LEVEL", 10, minimum=0)
FFMPEG_NORMALIZE_TIMEOUT = _env_int("FFMPEG_NORMALIZE_TIMEOUT", 1800, minimum=30)
FFMPEG_LOOP_PREENCODE_TIMEOUT = _env_int("FFMPEG_LOOP_PREENCODE_TIMEOUT", 120, minimum=15)

_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value < minimum or value > maximum:
        log.warning(
            "Invalid %s=%s, expected %.2f..%.2f; using default %s",
            name,
            raw,
            minimum,
            maximum,
            default,
        )
        return default
    return value


NORMALIZE_TARGET_I_VALUE = _env_float("NORMALIZE_TARGET_I", -16.0, -70.0, -5.0)
NORMALIZE_TARGET_LRA_VALUE = _env_float("NORMALIZE_TARGET_LRA", 11.0, 1.0, 50.0)
NORMALIZE_TARGET_TP_VALUE = _env_float("NORMALIZE_TARGET_TP", -1.5, -9.0, 0.0)


def _archive_destination(source: Path) -> Path:
    dest = ARCHIVE_DIR / source.name
    if not dest.exists():
        return dest
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return ARCHIVE_DIR / f"{source.stem}_{timestamp}{source.suffix}"


def _normalization_signature(track: Path) -> str:
    st = track.stat()
    payload = f"{track.name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def _normalized_path(track: Path) -> Path:
    return NORMALIZED_DIR / f"{track.stem}.{_normalization_signature(track)}.wav"


def source_tracks() -> list[Path]:
    tracks: list[Path] = []
    for p in sorted(MUSIC_DIR.glob("*.wav")):
        if not p.is_file() or p.is_symlink():
            continue
        if _CONTROL_CHAR_PATTERN.search(p.name):
            log.warning("Skipping unsafe filename containing control chars: %r", p.name)
            continue
        tracks.append(p)
    return tracks


def stream_ready_tracks(tracks: list[Path]) -> list[Path]:
    ready = [p for p in (_normalized_path(track) for track in tracks) if p.exists()]
    ready.sort()
    random.shuffle(ready)
    return ready


def archive_old_files() -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now() - timedelta(days=ARCHIVE_DAYS)
    moved = 0
    for f in source_tracks():
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


def prune_normalized_cache() -> int:
    if not NORMALIZED_DIR.exists():
        return 0
    expected = {_normalized_path(track).name for track in source_tracks()}
    removed = 0
    for cached in NORMALIZED_DIR.glob("*.wav"):
        if not cached.is_file() or cached.is_symlink():
            continue
        if cached.name in expected:
            continue
        cached.unlink(missing_ok=True)
        removed += 1
    if removed:
        log.info("Pruned %d stale normalized track(s)", removed)
    return removed


def _normalize_track(track: Path, normalized_path: Path) -> bool:
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(NORMALIZED_DIR))
    os.close(fd)
    tmp = Path(tmp_path)

    filter_graph = (
        f"loudnorm=I={NORMALIZE_TARGET_I_VALUE}:"
        f"LRA={NORMALIZE_TARGET_LRA_VALUE}:"
        f"TP={NORMALIZE_TARGET_TP_VALUE}:"
        "dual_mono=true"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        "1",
        "-i",
        str(track),
        "-vn",
        "-sn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-af",
        filter_graph,
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=FFMPEG_NORMALIZE_TIMEOUT,
            preexec_fn=lambda: os.nice(NORMALIZE_NICE_LEVEL),
        )
        tmp.replace(normalized_path)
        log.info("Normalized: %s -> %s", track.name, normalized_path.name)
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        log.warning("Failed to normalize %s: %s", track.name, stderr.strip())
        tmp.unlink(missing_ok=True)
        return False
    except subprocess.TimeoutExpired:
        log.warning("Normalization timed out for %s after %ds", track.name, FFMPEG_NORMALIZE_TIMEOUT)
        tmp.unlink(missing_ok=True)
        return False


def normalize_tracks(max_files: int) -> int:
    normalized = 0
    for track in source_tracks():
        normalized_path = _normalized_path(track)
        if normalized_path.exists():
            continue
        if _normalize_track(track, normalized_path):
            normalized += 1
        if normalized >= max_files:
            break
    return normalized


def run_idle_maintenance() -> tuple[int, int, int]:
    archived = archive_old_files()
    pruned = prune_archive()
    removed_cache = prune_normalized_cache()
    return archived, pruned, removed_cache


def build_playlist(tracks: list[Path], tmpdir: str) -> Path:
    playlist = Path(tmpdir) / "playlist.txt"
    with open(playlist, "w", encoding="utf-8") as f:
        for track in tracks:
            safe = str(track).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    log.info("Playlist: %d tracks", len(tracks))
    return playlist


def _valid_rtmp_target(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"rtmp", "rtmps"}:
        return False
    return bool(parsed.netloc)


def _valid_stream_key(key: str) -> bool:
    if not key:
        return False
    if any(ch in key for ch in "|[]"):
        return False
    if any(ord(ch) < 32 for ch in key):
        return False
    return key == key.strip()


def _build_target(base_url: str, stream_key: str, platform: str) -> str | None:
    base_url = base_url.rstrip("/")
    stream_key = stream_key.strip()
    target = f"{base_url}/{stream_key}"

    if not _valid_rtmp_target(target):
        log.warning("Invalid %s URL; expected rtmp/rtmps scheme", platform)
        return None
    if not _valid_stream_key(stream_key):
        log.warning("Invalid %s stream key format", platform)
        return None
    return target


def build_outputs() -> str:
    outputs = []

    if YOUTUBE_URL and YOUTUBE_KEY:
        yt = _build_target(YOUTUBE_URL, YOUTUBE_KEY, "YouTube")
        if yt:
            outputs.append(f"[f=flv]{yt}")
            log.info("YouTube output configured")

    if KICK_URL and KICK_KEY:
        kick = _build_target(KICK_URL, KICK_KEY, "Kick")
        if kick:
            outputs.append(f"[f=flv]{kick}")
            log.info("Kick output configured")

    if not outputs:
        log.error("No stream destinations configured. Set YOUTUBE_URL/KEY or KICK_URL/KEY.")
        sys.exit(1)

    return "|".join(outputs)


LOOP_VIDEO = Path("/tmp/loop.flv")


def _ensure_loop_video() -> None:
    if not BACKGROUND.exists():
        log.error("Background image not found: %s", BACKGROUND)
        sys.exit(1)
    if LOOP_VIDEO.exists() and LOOP_VIDEO.stat().st_mtime >= BACKGROUND.stat().st_mtime:
        return
    if LOOP_VIDEO.exists():
        log.info("Background updated; regenerating loop video")

    log.info("Pre-encoding loop video from %s ...", BACKGROUND.name)
    fd, tmp_path = tempfile.mkstemp(suffix=".flv")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(BACKGROUND),
                "-c:v",
                "libx264",
                "-tune",
                "stillimage",
                "-preset",
                "ultrafast",
                "-b:v",
                "500k",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=1280:720",
                "-r",
                "5",
                "-g",
                "10",
                "-t",
                "10",
                "-an",
                "-f",
                "flv",
                str(tmp),
            ],
            check=True,
            capture_output=True,
            timeout=FFMPEG_LOOP_PREENCODE_TIMEOUT,
        )
        tmp.replace(LOOP_VIDEO)
        log.info("Loop video ready: %s (%.1f MB)", LOOP_VIDEO, LOOP_VIDEO.stat().st_size / 1e6)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        tmp.unlink(missing_ok=True)
        log.error("Failed to pre-encode loop video: %s", stderr.strip())
        raise
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        log.error("Loop video pre-encode timed out after %ds", FFMPEG_LOOP_PREENCODE_TIMEOUT)
        raise


def run_ffmpeg(playlist: Path, output_tee: str) -> int:
    _ensure_loop_video()

    cmd = [
        "ffmpeg",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        str(LOOP_VIDEO),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(playlist),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-shortest",
        "-f",
        "tee",
        "-map",
        "0:v",
        "-map",
        "1:a",
        output_tee,
    ]

    log.info("Starting ffmpeg stream...")
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
        next_normalize_due = time.monotonic() + NORMALIZE_DURING_STREAM_INTERVAL

        while True:
            rc = proc.poll()
            if rc is not None:
                return rc

            now = time.monotonic()
            if now >= next_normalize_due:
                normalized = normalize_tracks(max_files=NORMALIZE_MAX_FILES_PER_CYCLE)
                if normalized:
                    log.info("In-stream maintenance normalized %d track(s)", normalized)
                next_normalize_due = now + NORMALIZE_DURING_STREAM_INTERVAL
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stream interrupted by user")
        raise
    except Exception as e:
        log.error("ffmpeg error: %s", e)
        return 1
    finally:
        if proc and proc.poll() is None:
            log.info("Stopping ffmpeg process...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg did not stop gracefully; killing process")
                proc.kill()
                proc.wait(timeout=5)


def main() -> None:
    log.info("=== Headless Streamer starting ===")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)

    output_tee = build_outputs()
    restart_delay = RESTART_DELAY

    while True:
        try:
            archived, pruned, removed_cache = run_idle_maintenance()
            log.debug(
                "Idle maintenance summary: archived=%d pruned=%d stale_normalized=%d",
                archived,
                pruned,
                removed_cache,
            )

            sources = source_tracks()
            if not sources:
                log.warning("No tracks found in %s. Waiting %ds...", MUSIC_DIR, WAIT_NO_MUSIC)
                time.sleep(WAIT_NO_MUSIC)
                continue

            bootstrap_normalized = normalize_tracks(max_files=NORMALIZE_BOOTSTRAP_BATCH)
            if bootstrap_normalized:
                log.info("Bootstrap normalized %d track(s)", bootstrap_normalized)

            tracks = stream_ready_tracks(sources)
            if not tracks:
                log.warning(
                    "No normalized tracks ready yet. Waiting %ds for background normalization...",
                    WAIT_NO_MUSIC,
                )
                time.sleep(WAIT_NO_MUSIC)
                continue

            with tempfile.TemporaryDirectory() as tmpdir:
                playlist = build_playlist(tracks, tmpdir)
                rc = run_ffmpeg(playlist, output_tee)
                log.info("ffmpeg exited with code %d", rc)
                restart_delay = RESTART_DELAY if rc == 0 else min(restart_delay * 2, MAX_RESTART_DELAY)

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
