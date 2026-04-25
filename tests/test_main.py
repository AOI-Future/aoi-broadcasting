import os
import tempfile
import unittest
from pathlib import Path

import main


class StreamerSecurityTests(unittest.TestCase):
    def test_valid_stream_key_rejects_control_chars(self):
        self.assertFalse(main._valid_stream_key("abc\n123"))
        self.assertFalse(main._valid_stream_key(" bad"))
        self.assertTrue(main._valid_stream_key("abc123-OK"))

    def test_env_int_bounds_and_fallback(self):
        old = os.environ.get("TEST_INT")
        try:
            os.environ["TEST_INT"] = "0"
            self.assertEqual(main._env_int("TEST_INT", 10, minimum=1), 1)
            os.environ["TEST_INT"] = "5"
            self.assertEqual(main._env_int("TEST_INT", 10, minimum=1), 5)
            os.environ["TEST_INT"] = "NaN?"
            self.assertEqual(main._env_int("TEST_INT", 10, minimum=1), 10)
        finally:
            if old is None:
                os.environ.pop("TEST_INT", None)
            else:
                os.environ["TEST_INT"] = old

    def test_source_tracks_skips_unsafe_and_symlink(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            safe = root / "safe.wav"
            unsafe = root / "unsafe\nname.wav"
            safe.write_bytes(b"RIFF")
            unsafe.write_bytes(b"RIFF")
            (root / "link.wav").symlink_to(safe)

            main.MUSIC_DIR = root
            try:
                tracks = main.source_tracks()
            finally:
                main.MUSIC_DIR = old_music

        names = [p.name for p in tracks]
        self.assertEqual(names, ["safe.wav"])


    def test_archive_disabled_by_default(self):
        self.assertFalse(main.ARCHIVE_ENABLED)
        self.assertEqual(main.archive_old_files(), 0)
        self.assertEqual(main.prune_archive(), 0)


class MusicDirFingerprintTests(unittest.TestCase):
    def test_empty_directory(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            main.MUSIC_DIR = Path(d)
            try:
                fp = main._music_dir_fingerprint()
            finally:
                main.MUSIC_DIR = old_music
        self.assertIsInstance(fp, str)
        self.assertEqual(len(fp), 64)

    def test_stable_with_same_files(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.wav").write_bytes(b"RIFF" * 100)
            (root / "b.wav").write_bytes(b"RIFF" * 200)
            main.MUSIC_DIR = root
            try:
                fp1 = main._music_dir_fingerprint()
                fp2 = main._music_dir_fingerprint()
            finally:
                main.MUSIC_DIR = old_music
        self.assertEqual(fp1, fp2)

    def test_changes_on_file_add(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.wav").write_bytes(b"RIFF" * 100)
            main.MUSIC_DIR = root
            try:
                fp_before = main._music_dir_fingerprint()
                (root / "b.wav").write_bytes(b"RIFF" * 200)
                fp_after = main._music_dir_fingerprint()
            finally:
                main.MUSIC_DIR = old_music
        self.assertNotEqual(fp_before, fp_after)

    def test_ignores_symlinks(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            real = root / "real.wav"
            real.write_bytes(b"RIFF" * 100)
            main.MUSIC_DIR = root
            try:
                fp_before = main._music_dir_fingerprint()
                (root / "link.wav").symlink_to(real)
                fp_after = main._music_dir_fingerprint()
            finally:
                main.MUSIC_DIR = old_music
        self.assertEqual(fp_before, fp_after)


class VideoClipTests(unittest.TestCase):
    def test_source_video_clips_empty_when_no_video_dir(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            main.MUSIC_DIR = Path(d)
            try:
                clips = main.source_video_clips()
            finally:
                main.MUSIC_DIR = old_music
        self.assertEqual(clips, [])

    def test_source_video_clips_skips_unsafe_and_symlink(self):
        old_music = main.MUSIC_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            video_dir = root / "video"
            video_dir.mkdir()
            safe = video_dir / "clip.mp4"
            unsafe = video_dir / "bad\nname.mp4"
            safe.write_bytes(b"FTYP")
            unsafe.write_bytes(b"FTYP")
            (video_dir / "link.mp4").symlink_to(safe)

            main.MUSIC_DIR = root
            try:
                clips = main.source_video_clips()
            finally:
                main.MUSIC_DIR = old_music

        self.assertEqual([p.name for p in clips], ["clip.mp4"])

    def test_build_video_playlist_repeat_count(self):
        old_repeat = main.VIDEO_REPEAT_COUNT
        main.VIDEO_REPEAT_COUNT = 3
        try:
            with tempfile.TemporaryDirectory() as d:
                clips = [Path(d) / "a.mp4", Path(d) / "b.mp4"]
                for c in clips:
                    c.write_bytes(b"FTYP")
                playlist = main.build_video_playlist(clips, d)
                lines = playlist.read_text().strip().splitlines()
                self.assertEqual(len(lines), 6)  # 2 clips × 3 repeats
        finally:
            main.VIDEO_REPEAT_COUNT = old_repeat


class ChunkPlaylistTests(unittest.TestCase):
    def test_chunk_playlist_single_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            tracks = []
            for name in ("a.wav", "b.wav", "c.wav"):
                p = Path(d) / name
                p.write_bytes(b"RIFF")
                tracks.append(p)
            playlist = main.build_playlist(tracks, d, repeat=1)
            lines = playlist.read_text().strip().splitlines()
            self.assertEqual(len(lines), 3)

    def test_chunk_playlist_multi_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            tracks = []
            for name in ("a.wav", "b.wav"):
                p = Path(d) / name
                p.write_bytes(b"RIFF")
                tracks.append(p)
            playlist = main.build_playlist(tracks, d, repeat=3)
            lines = playlist.read_text().strip().splitlines()
            self.assertEqual(len(lines), 6)


if __name__ == "__main__":
    unittest.main()
