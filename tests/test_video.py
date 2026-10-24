"""Tests for Pass 4: torrent video-name duplicates."""

import tempfile
import unittest
from pathlib import Path

from dedupe.cli import run_dedupe
from dedupe.core import TrashManager
from dedupe.fuzzy import run_fuzzy_pass
from dedupe.video import parse_video_name, run_video_pass
import io
from unittest.mock import patch


class TestVideoPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, size):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def _run(self):
        tm = TrashManager(self.root)
        return run_video_pass(self.root, tm, removed_folders=set(), removed_files=set())

    def test_parse_release_name(self):
        parsed = parse_video_name("www.yts.mx - The.Movie.2020.1080p.BluRay.x264-YTS")
        self.assertEqual(parsed.title, "the movie")
        self.assertEqual(parsed.year, 2020)
        self.assertEqual(parsed.resolution, 1080)

    def test_highest_resolution_keeper_across_folders(self):
        low = self._write("a/The.Movie.2020.720p.WEB-DL.x264.mkv", 5000)
        high = self._write("b/The Movie (2020) [1080p].mp4", 1000)
        removals, removed, _ = self._run()
        video_removals = [r for r in removals if r.reason == "video-name"]
        self.assertEqual(len(video_removals), 1)
        self.assertEqual(video_removals[0].kept_path, high)
        self.assertEqual(video_removals[0].removed_path, low)
        self.assertFalse(low.exists())
        self.assertTrue(high.exists())

    def test_sequel_and_year_and_episode_guards(self):
        self._write("Rocky.IV.1985.720p.mkv", 1000)
        self._write("other/Rocky.V.1985.720p.mkv", 1000)
        self._write("Show.S01E01.720p.mkv", 1000)
        self._write("other/Show.S01E02.720p.mkv", 1000)
        self._write("Film.2019.1080p.mkv", 1000)
        self._write("other/Film.2020.1080p.mkv", 1000)
        removals, _, _ = self._run()
        self.assertEqual(removals, [])

    def test_fuzzy_roman_guard(self):
        self._write("Rocky.IV.720p.mkv", 1000)
        self._write("Rocky.V.720p.mkv", 1000)
        tm = TrashManager(self.root)
        removals, _ = run_fuzzy_pass(self.root, tm, removed_folders=set(), removed_files=set())
        self.assertEqual(removals, [])

    def test_sample_and_leftover_and_kept_folder(self):
        movie = self._write("TorrentA/Movie.2021.1080p.mkv", 10000)
        dupe = self._write("TorrentB/Movie.2021.720p.mkv", 9000)
        self._write("TorrentB/movie.nfo", 10)
        self._write("TorrentB/poster.jpg", 10)
        sample = self._write("TorrentA/sample.mkv", 100)
        keep_doc = self._write("TorrentA/notes.pdf", 10)
        removals, _, _ = self._run()
        reasons = {(r.removed_path.name, r.reason) for r in removals}
        self.assertIn(("Movie.2021.720p.mkv", "video-name"), reasons)
        self.assertIn(("sample.mkv", "sample"), reasons)
        self.assertIn(("TorrentB", "torrent-leftover"), reasons)
        self.assertTrue(movie.exists())
        self.assertTrue((self.root / "TorrentA").exists())
        self.assertFalse((self.root / "TorrentB").exists())

    def test_cli_no_fuzzy_disables_video_and_nonvideo_untouched(self):
        self._write("a/Movie.2020.1080p.mkv", 2000)
        self._write("b/Movie.2020.720p.mkv", 2000)
        self._write("b/readme.txt", 10)
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            ret = run_dedupe(str(self.root), no_fuzzy=True)
        self.assertEqual(ret, 0)
        self.assertTrue((self.root / "b" / "Movie.2020.720p.mkv").exists())


if __name__ == "__main__":
    unittest.main()
