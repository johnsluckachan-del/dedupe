"""Tests for progress output, --quiet, and lazy single hashing."""

import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import dedupe.core as core
from dedupe.cli import main, parse_args, run_dedupe


class TestProgressAndHashing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode())
        return p

    def test_progress_on_stderr_and_done_last(self):
        self._write("a.txt", "unique-content")
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            ret = run_dedupe(str(self.root), dry_run=True)
        self.assertEqual(ret, 0)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("Scanning "))
        self.assertIn(str(self.root), lines[0])
        self.assertTrue(lines[-1].startswith("Done"))
        self.assertIn("Summary:", out.getvalue())
        self.assertNotIn("Scanning", out.getvalue())

    def test_quiet_silences_progress_but_errors_remain(self):
        args = parse_args([str(self.root), "--quiet"])
        self.assertTrue(args.quiet)
        self._write("a.txt", "x")
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            self.assertEqual(main([str(self.root), "--dry-run", "--quiet"]), 0)
        self.assertEqual(err.getvalue(), "")
        err2 = io.StringIO()
        with patch("sys.stderr", err2):
            self.assertEqual(run_dedupe(str(self.root / "nope"), quiet=True), 1)
        self.assertIn("does not exist", err2.getvalue())
        self.assertNotIn("Scanning", err2.getvalue())

    def test_unique_size_never_hashed_and_hash_at_most_once(self):
        self._write("unique.txt", "12345")  # size 5, unique
        self._write("dirA/f.txt", "same-data")
        self._write("dirB/f.txt", "same-data")
        self._write("solo.txt", "same-data")  # exact twin outside folders
        counts = Counter()
        real = core.hash_file

        def counting(path, *a, **k):
            counts[Path(path)] += 1
            return real(path, *a, **k)

        with patch("dedupe.core.hash_file", side_effect=counting), patch(
            "dedupe.folder.hash_file", side_effect=counting
        ), patch("dedupe.exact.hash_file", side_effect=counting):
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(run_dedupe(str(self.root), dry_run=True), 0)
        self.assertNotIn(self.root / "unique.txt", counts)
        self.assertTrue(all(v == 1 for v in counts.values()), dict(counts))
        self.assertIn(self.root / "dirA" / "f.txt", counts)

    def test_nonmatching_folders_not_hashed(self):
        self._write("a/one.txt", "aaa")
        self._write("b/two.txt", "bbbb")
        counts = Counter()
        real = core.hash_file

        def counting(path, *a, **k):
            counts[Path(path)] += 1
            return real(path, *a, **k)

        with patch("dedupe.core.hash_file", side_effect=counting), patch(
            "dedupe.folder.hash_file", side_effect=counting
        ), patch("dedupe.exact.hash_file", side_effect=counting):
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(run_dedupe(str(self.root), dry_run=True), 0)
        self.assertEqual(dict(counts), {})


if __name__ == "__main__":
    unittest.main()
