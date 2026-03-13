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


if __name__ == "__main__":
    unittest.main()
