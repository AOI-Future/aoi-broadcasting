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
import threading
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
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

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


ARCHIVE_ENABLED = os.environ.get("ARCHIVE_ENABLED", "false").lower() in ("true", "1", "yes")
ARCHIVE_DAYS = _env_int("ARCHIVE_DAYS", 30, minimum=1)
ARCHIVE_RETENTION_DAYS = _env_int("ARCHIVE_RETENTION_DAYS", 0, minimum=0)
WAIT_NO_MUSIC = _env_int("WAIT_NO_MUSIC", 30, minimum=5)
RESTART_DELAY = _env_int("RESTART_DELAY", 5, minimum=1)
MAX_RESTART_DELAY = _env_int("MAX_RESTART_DELAY", 60, minimum=5)
NORMALIZE_MAX_FILES_PER_CYCLE = _env_int("NORMALIZE_MAX_FILES_PER_CYCLE", 8, minimum=1)
NORMALIZE_BOOTSTRAP_BATCH = _env_int("NORMALIZE_BOOTSTRAP_BATCH", 50, minimum=1)
NORMALIZE_DURING_STREAM_INTERVAL = _env_int("NORMALIZE_DURING_STREAM_INTERVAL", 120, minimum=30)
NORMALIZE_TARGET_I = os.environ.get("NORMALIZE_TARGET_I", "-16").strip() or "-16"
NORMALIZE_TARGET_LRA = os.environ.get("NORMALIZE_TARGET_LRA", "11").strip() or "11"
NORMALIZE_TARGET_TP = os.environ.get("NORMALIZE_TARGET_TP", "-1.5").strip() or "-1.5"
NORMALIZE_NICE_LEVEL = _env_int("NORMALIZE_NICE_LEVEL", 10, minimum=0)
PLAYLIST_REPEAT_COUNT = _env_int("PLAYLIST_REPEAT_COUNT", 10, minimum=1)
PLAYLIST_CHUNK_SIZE = _env_int("PLAYLIST_CHUNK_SIZE", 5, minimum=0)
MUSIC_CHANGE_CHECK_INTERVAL = _env_int("MUSIC_CHANGE_CHECK_INTERVAL", 60, minimum=10)
MUSIC_CHANGE_DEBOUNCE = _env_int("MUSIC_CHANGE_DEBOUNCE", 60, minimum=0)
STREAM_HEARTBEAT_INTERVAL = _env_int("STREAM_HEARTBEAT_INTERVAL", 120, minimum=30)
FFMPEG_NORMALIZE_TIMEOUT = _env_int("FFMPEG_NORMALIZE_TIMEOUT", 1800, minimum=30)
FFMPEG_LOOP_PREENCODE_TIMEOUT = _env_int("FFMPEG_LOOP_PREENCODE_TIMEOUT", 120, minimum=15)
FFMPEG_VIDEO_ENCODE_TIMEOUT = _env_int("FFMPEG_VIDEO_ENCODE_TIMEOUT", 600, minimum=30)

VIDEO_CACHE_DIR = Path("/data/video_cache")
_VIDEO_EXTENSIONS = ("*.mp4", "*.mov", "*.mkv", "*.avi", "*.webm")
VIDEO_FPS = os.environ.get("VIDEO_FPS", "24").strip() or "24"
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "1500k").strip() or "1500k"
VIDEO_REPEAT_COUNT = _env_int("VIDEO_REPEAT_COUNT", 500, minimum=1)
VIDEO_SHUFFLE = os.environ.get("VIDEO_SHUFFLE", "true").lower() in ("true", "1", "yes")

_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def _archive_destination(source: Path) -> Path:
    dest = ARCHIVE_DIR / source.name
    if not dest.exists():
        return dest
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return ARCHIVE_DIR / f"{source.stem}_{timestamp}{source.suffix}"


def _normalization_signature(track: Path) -> str:
    st = track.stat()
    payload = f"{track.name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _normalized_path(track: Path) -> Path:
    return NORMALIZED_DIR / f"{track.stem}.{_normalization_signature(track)}.wav"


_MUSIC_EXTENSIONS = ("*.wav", "*.flac", "*.mp3")


def _music_dir_fingerprint() -> str:
    """Return a hash summarising the current state of the music directory."""
    entries: list[str] = []
    for ext in _MUSIC_EXTENSIONS:
        for p in MUSIC_DIR.glob(ext):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                st = p.stat()
                entries.append(f"{p.name}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                continue
    payload = "\n".join(sorted(entries)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def source_tracks() -> list[Path]:
    tracks: list[Path] = []
    for ext in _MUSIC_EXTENSIONS:
        for p in MUSIC_DIR.glob(ext):
            if not p.is_file() or p.is_symlink():
                continue
            if _CONTROL_CHAR_PATTERN.search(p.name):
                log.warning("Skipping unsafe filename containing control chars: %r", p.name)
                continue
            tracks.append(p)
    return sorted(tracks)


def stream_ready_tracks(tracks: list[Path]) -> list[Path]:
    ready = [p for p in (_normalized_path(track) for track in tracks) if p.exists()]
    ready.sort()
    random.shuffle(ready)
    return ready


def archive_old_files() -> int:
    if not ARCHIVE_ENABLED:
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now() - timedelta(days=ARCHIVE_DAYS)
    moved = 0
    for f in source_tracks():
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            dest = _archive_destination(f)
            try:
                shutil.copy2(str(f), str(dest))
            except OSError as e:
                log.warning("Archive copy failed %s: %s", f.name, e)
                continue
            try:
                f.unlink()
                log.info("Archived: %s -> %s (mtime: %s)", f.name, dest.name, mtime.isoformat())
                moved += 1
            except PermissionError:
                # Source is on a read-only mount; remove the copy to avoid
                # duplicating it every cycle.
                dest.unlink(missing_ok=True)
                log.debug("Cannot archive %s (source not deletable, skipping)", f.name)
    if moved:
        log.info("Archived %d file(s)", moved)
    return moved


def prune_archive() -> int:
    if not ARCHIVE_ENABLED or ARCHIVE_RETENTION_DAYS <= 0:
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


def source_video_clips() -> list[Path]:
    video_dir = MUSIC_DIR / "video"
    if not video_dir.exists():
        return []
    clips: list[Path] = []
    for pattern in _VIDEO_EXTENSIONS:
        for p in video_dir.glob(pattern):
            if not p.is_file() or p.is_symlink():
                continue
            if _CONTROL_CHAR_PATTERN.search(p.name):
                log.warning("Skipping unsafe video filename: %r", p.name)
                continue
            clips.append(p)
    return sorted(clips)


def _video_signature(clip: Path) -> str:
    st = clip.stat()
    payload = f"{clip.name}:{st.st_size}:{st.st_mtime_ns}:{VIDEO_FPS}:{VIDEO_BITRATE}".encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def _video_cache_path(clip: Path) -> Path:
    return VIDEO_CACHE_DIR / f"{clip.stem}.{_video_signature(clip)}.flv"


def _encode_video_clip(clip: Path, out: Path) -> bool:
    VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".flv", dir=str(VIDEO_CACHE_DIR))
    os.close(fd)
    tmp = Path(tmp_path)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(clip),
        "-vf", (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            "format=yuv420p"
        ),
        "-c:v", "libx264",
        "-preset", "slow",
        "-b:v", VIDEO_BITRATE,
        "-r", VIDEO_FPS,
        "-g", str(int(float(VIDEO_FPS)) * 2),
        "-color_range", "tv",
        "-an",
        "-f", "flv",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=FFMPEG_VIDEO_ENCODE_TIMEOUT)
        tmp.replace(out)
        log.info("Video clip encoded: %s -> %s", clip.name, out.name)
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        log.warning("Failed to encode video %s: %s", clip.name, stderr.strip())
        tmp.unlink(missing_ok=True)
        return False
    except subprocess.TimeoutExpired:
        log.warning("Video encode timed out for %s after %ds", clip.name, FFMPEG_VIDEO_ENCODE_TIMEOUT)
        tmp.unlink(missing_ok=True)
        return False


def ensure_video_cache(clips: list[Path]) -> list[Path]:
    """Pre-encode clips to consistent spec. Returns list of ready cached paths."""
    ready: list[Path] = []
    for clip in clips:
        cached = _video_cache_path(clip)
        if not cached.exists():
            log.info("Pre-encoding video: %s ...", clip.name)
            _encode_video_clip(clip, cached)
        if cached.exists():
            ready.append(cached)
    return ready


def prune_video_cache() -> int:
    if not VIDEO_CACHE_DIR.exists():
        return 0
    expected = {_video_cache_path(clip).name for clip in source_video_clips()}
    removed = 0
    for cached in VIDEO_CACHE_DIR.glob("*.flv"):
        if not cached.is_file() or cached.is_symlink():
            continue
        if cached.name in expected:
            continue
        cached.unlink(missing_ok=True)
        removed += 1
    if removed:
        log.info("Pruned %d stale video cache file(s)", removed)
    return removed


def _normalize_track(track: Path, normalized_path: Path) -> bool:
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(NORMALIZED_DIR))
    os.close(fd)
    tmp = Path(tmp_path)

    filter_graph = (
        f"loudnorm=I={NORMALIZE_TARGET_I}:"
        f"LRA={NORMALIZE_TARGET_LRA}:"
        f"TP={NORMALIZE_TARGET_TP}:"
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


def run_idle_maintenance() -> tuple[int, int, int, int]:
    archived = archive_old_files()
    pruned = prune_archive()
    removed_cache = prune_normalized_cache()
    removed_video = prune_video_cache()
    return archived, pruned, removed_cache, removed_video


class BackgroundNormalizer:
    """Run normalization in a background thread so it never blocks streaming."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_count = 0

    def start(self, max_files: int = NORMALIZE_BOOTSTRAP_BATCH) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, args=(max_files,), daemon=True
            )
            self._thread.start()

    def _run(self, max_files: int) -> None:
        try:
            normalized = 0
            for track in source_tracks():
                if self._stop_event.is_set():
                    break
                normalized_path = _normalized_path(track)
                if normalized_path.exists():
                    continue
                if _normalize_track(track, normalized_path):
                    normalized += 1
                if normalized >= max_files:
                    break
            with self._lock:
                self._last_count = normalized
            if normalized:
                log.info("Background normalizer finished: %d track(s)", normalized)
        except Exception as e:
            log.warning("Background normalizer error: %s", e)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._thread is not None:
                self._thread.join(timeout=5)
                self._thread = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_count(self) -> int:
        with self._lock:
            return self._last_count


def build_playlist(tracks: list[Path], tmpdir: str, repeat: int | None = None) -> Path:
    playlist = Path(tmpdir) / "playlist.txt"
    repeat_count = repeat if repeat is not None else PLAYLIST_REPEAT_COUNT
    with open(playlist, "w", encoding="utf-8") as f:
        for i in range(repeat_count):
            cycle = tracks.copy()
            if i > 0:
                random.shuffle(cycle)
            for track in cycle:
                safe = str(track).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
    total = len(tracks) * repeat_count
    log.info("Playlist: %d tracks (%d unique × %d repeat cycles)", total, len(tracks), repeat_count)
    return playlist


def build_video_playlist(cached_clips: list[Path], tmpdir: str) -> Path:
    playlist = Path(tmpdir) / "video_playlist.txt"
    with open(playlist, "w", encoding="utf-8") as f:
        for i in range(VIDEO_REPEAT_COUNT):
            cycle = cached_clips.copy()
            if VIDEO_SHUFFLE and i > 0:
                random.shuffle(cycle)
            for clip in cycle:
                safe = str(clip).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
    total = len(cached_clips) * VIDEO_REPEAT_COUNT
    log.info(
        "Video playlist: %d entries (%d clips × %d repeats)",
        total, len(cached_clips), VIDEO_REPEAT_COUNT,
    )
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
            outputs.append(f"[f=flv:onfail=ignore]{yt}")
            log.info("YouTube output configured")

    if KICK_URL and KICK_KEY:
        kick = _build_target(KICK_URL, KICK_KEY, "Kick")
        if kick:
            outputs.append(f"[f=flv:onfail=ignore]{kick}")
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
                "-color_range",
                "tv",
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


def run_ffmpeg(
    playlist: Path,
    output_tee: str,
    fingerprint: str | None = None,
    normalizer: BackgroundNormalizer | None = None,
    video_playlist: Path | None = None,
) -> tuple[int, bool]:
    """Run ffmpeg for one playlist chunk.

    Returns ``(return_code, music_changed)`` where *music_changed* is
    ``True`` when a directory change was detected and ffmpeg was stopped
    early so the caller can rebuild the playlist.
    """
    if video_playlist is not None:
        video_input_args = [
            "-re",
            "-f", "concat",
            "-safe", "0",
            "-i", str(video_playlist),
        ]
        log.info("Video source: clip playlist (%s)", video_playlist.name)
    else:
        _ensure_loop_video()
        video_input_args = [
            "-re",
            "-stream_loop", "-1",
            "-i", str(LOOP_VIDEO),
        ]
        log.info("Video source: static background loop")

    cmd = [
        "ffmpeg",
        *video_input_args,
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
    try:
        HEALTHCHECK_FILE.write_text(str(time.time()))
    except OSError:
        pass
    proc: subprocess.Popen | None = None
    music_changed = False
    try:
        proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
        next_normalize_due = time.monotonic() + NORMALIZE_DURING_STREAM_INTERVAL
        next_heartbeat = time.monotonic() + STREAM_HEARTBEAT_INTERVAL
        next_music_check = time.monotonic() + MUSIC_CHANGE_CHECK_INTERVAL
        stream_start = time.monotonic()
        pending_change_fp: str | None = None
        pending_change_time: float = 0.0

        while True:
            rc = proc.poll()
            if rc is not None:
                return rc, music_changed

            now = time.monotonic()
            if now >= next_heartbeat:
                elapsed = int(now - stream_start)
                log.info("Stream heartbeat: ffmpeg running (%dm%02ds)", elapsed // 60, elapsed % 60)
                try:
                    HEALTHCHECK_FILE.write_text(str(time.time()))
                except OSError:
                    pass
                next_heartbeat = now + STREAM_HEARTBEAT_INTERVAL
            if now >= next_normalize_due:
                if normalizer is not None:
                    normalizer.start(max_files=NORMALIZE_MAX_FILES_PER_CYCLE)
                else:
                    normalized = normalize_tracks(max_files=NORMALIZE_MAX_FILES_PER_CYCLE)
                    if normalized:
                        log.info("In-stream maintenance normalized %d track(s)", normalized)
                next_normalize_due = now + NORMALIZE_DURING_STREAM_INTERVAL

            # Music directory change detection
            if fingerprint is not None and now >= next_music_check:
                new_fp = _music_dir_fingerprint()
                if new_fp != fingerprint and pending_change_fp is None:
                    log.info("Music directory change detected, waiting %ds to confirm...", MUSIC_CHANGE_DEBOUNCE)
                    pending_change_fp = new_fp
                    pending_change_time = now
                elif pending_change_fp is not None:
                    if now - pending_change_time >= MUSIC_CHANGE_DEBOUNCE:
                        stable_fp = _music_dir_fingerprint()
                        if stable_fp != fingerprint:
                            log.info("Music directory changed (confirmed), stopping current chunk")
                            music_changed = True
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=5)
                            return 0, True
                        else:
                            log.info("Music directory change reverted, resuming")
                            pending_change_fp = None
                next_music_check = now + MUSIC_CHANGE_CHECK_INTERVAL

            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stream interrupted by user")
        raise
    except Exception as e:
        log.error("ffmpeg error: %s", e)
        return 1, music_changed
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
    normalizer = BackgroundNormalizer()
    first_boot = True

    while True:
        try:
            archived, pruned, removed_cache, removed_video = run_idle_maintenance()
            log.debug(
                "Idle maintenance summary: archived=%d pruned=%d stale_normalized=%d stale_video=%d",
                archived,
                pruned,
                removed_cache,
                removed_video,
            )

            sources = source_tracks()
            if not sources:
                log.warning("No tracks found in %s. Waiting %ds...", MUSIC_DIR, WAIT_NO_MUSIC)
                time.sleep(WAIT_NO_MUSIC)
                continue

            # First boot: synchronously normalize just enough tracks to start streaming,
            # then kick off background normalizer for the rest.
            # Subsequent cycles: normalize entirely in background.
            bootstrap_min = max(PLAYLIST_CHUNK_SIZE, 1)
            if first_boot:
                bootstrap_normalized = normalize_tracks(max_files=bootstrap_min)
                if bootstrap_normalized:
                    log.info("Bootstrap normalized %d track(s) (fast start)", bootstrap_normalized)
                first_boot = False
            normalizer.start(max_files=NORMALIZE_BOOTSTRAP_BATCH)

            tracks = stream_ready_tracks(sources)
            if not tracks:
                log.warning(
                    "No normalized tracks ready yet. Waiting %ds for background normalization...",
                    WAIT_NO_MUSIC,
                )
                time.sleep(WAIT_NO_MUSIC)
                continue

            # Pre-encode any new video clips; pick up additions each cycle automatically
            raw_clips = source_video_clips()
            if raw_clips:
                ready_clips = ensure_video_cache(raw_clips)
                log.info("Video: %d/%d clip(s) ready", len(ready_clips), len(raw_clips))
            else:
                ready_clips = []
                log.info("Video: no clips in %s/video/, using static background", MUSIC_DIR)

            with tempfile.TemporaryDirectory() as tmpdir:
                video_pl = build_video_playlist(ready_clips, tmpdir) if ready_clips else None

                if PLAYLIST_CHUNK_SIZE > 0:
                    # Chunk-based playback
                    fingerprint = _music_dir_fingerprint()
                    for i in range(0, len(tracks), PLAYLIST_CHUNK_SIZE):
                        chunk = tracks[i : i + PLAYLIST_CHUNK_SIZE]
                        log.info(
                            "Playing chunk %d/%d (%d tracks)",
                            i // PLAYLIST_CHUNK_SIZE + 1,
                            (len(tracks) + PLAYLIST_CHUNK_SIZE - 1) // PLAYLIST_CHUNK_SIZE,
                            len(chunk),
                        )
                        playlist = build_playlist(chunk, tmpdir, repeat=1)
                        rc, music_changed = run_ffmpeg(
                            playlist, output_tee,
                            fingerprint=fingerprint,
                            normalizer=normalizer,
                            video_playlist=video_pl,
                        )
                        log.info("ffmpeg exited with code %d", rc)
                        restart_delay = RESTART_DELAY if rc == 0 else min(restart_delay * 2, MAX_RESTART_DELAY)
                        if music_changed:
                            log.info("Music directory changed, rebuilding playlist")
                            break
                    # End of all chunks or music changed → restart outer loop
                else:
                    # Legacy mode: single giant playlist
                    playlist = build_playlist(tracks, tmpdir)
                    rc, _changed = run_ffmpeg(
                        playlist, output_tee,
                        normalizer=normalizer,
                        video_playlist=video_pl,
                    )
                    log.info("ffmpeg exited with code %d", rc)
                    restart_delay = RESTART_DELAY if rc == 0 else min(restart_delay * 2, MAX_RESTART_DELAY)

            log.info("Restarting cycle in %ds...", restart_delay)
            time.sleep(restart_delay)

        except KeyboardInterrupt:
            log.info("Shutting down gracefully")
            normalizer.stop()
            break
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)
            log.info("Retrying in %ds...", RESTART_DELAY)
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
