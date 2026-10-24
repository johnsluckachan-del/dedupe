"""Tests for CLI arguments, exit codes, and output formatting."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cli import main, parse_args, run_dedupe


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_args_defaults(self):
        args = parse_args(["some/path"])
        self.assertEqual(args.path, "some/path")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.permanent)
        self.assertFalse(args.no_fuzzy)
        self.assertAlmostEqual(args.threshold, 0.9)
        self.assertEqual(args.min_match, 10)

    def test_parse_args_flags(self):
        args = parse_args(
            [
                "some/path",
                "--dry-run",
                "--permanent",
                "--no-fuzzy",
                "--threshold",
                "0.75",
                "--min-match",
                "15",
            ]
        )
        self.assertEqual(args.path, "some/path")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.permanent)
        self.assertTrue(args.no_fuzzy)
        self.assertAlmostEqual(args.threshold, 0.75)
        self.assertEqual(args.min_match, 15)

    def test_parse_args_min_match_zero(self):
        args = parse_args(["some/path", "--min-match", "0"])
        self.assertEqual(args.min_match, 0)

    def test_nonexistent_path(self):
        bad_path = self.root / "does_not_exist"
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_dedupe(str(bad_path))
            self.assertEqual(ret, 1)
            self.assertIn("does not exist", mock_err.getvalue())

    def test_not_a_directory(self):
        file_path = self.root / "file.txt"
        file_path.write_text("hello")
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_dedupe(str(file_path))
            self.assertEqual(ret, 1)
            self.assertIn("is not a directory", mock_err.getvalue())

    def test_clean_folder_exits_zero_with_summary(self):
        f = self.root / "a.txt"
        f.write_text("unique content")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_dedupe(str(self.root))
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("Summary: 0 items removed (0 folder, 0 exact, 0 fuzzy), 0 bytes freed", out)

    def test_main_function(self):
        f = self.root / "a.txt"
        f.write_text("unique content")
        with patch("sys.stdout", new_callable=io.StringIO):
            ret = main([str(self.root), "--dry-run", "--min-match", "8"])
            self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
