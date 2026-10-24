"""Tests for CLI arguments, exit codes, and output formatting."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cli import build_parser, main, parse_args, run_dedupe


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


    def test_format_help(self):
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn("%LOCALAPPDATA%\\dedupe\\cache", help_text)
        self.assertIn("--cache-dir", help_text)


    def test_parse_args_report(self):
        args = parse_args(["some/path", "--report", "near-miss.csv"])
        self.assertEqual(args.report, "near-miss.csv")

    def test_report_csv_and_stderr(self):
        import csv
        vid_a = self.root / "a" / "The.Dark.Knight.2008.1080p.mkv"
        vid_b = self.root / "b" / "Dark Knight 2008 720p.mp4"
        vid_a.parent.mkdir(parents=True, exist_ok=True)
        vid_b.parent.mkdir(parents=True, exist_ok=True)
        vid_a.write_bytes(b"x" * 100)
        vid_b.write_bytes(b"y" * 100)

        report_path = self.root / "near-miss.csv"
        err = io.StringIO()
        with patch("sys.stderr", err):
            ret = main([str(self.root), "--dry-run", "--report", str(report_path)])
        self.assertEqual(ret, 0)
        self.assertTrue(report_path.exists())
        self.assertIn(f"Report: {report_path} (1 near-miss pairs)", err.getvalue())

        with open(report_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["blocked_by"], "below-threshold")
        self.assertTrue(0.5 <= float(rows[0]["score"]) < 0.85)

    def test_report_quiet_suppresses_stderr_line(self):
        report_path = self.root / "report.csv"
        err = io.StringIO()
        with patch("sys.stderr", err):
            ret = main([str(self.root), "--dry-run", "--quiet", "--report", str(report_path)])
        self.assertEqual(ret, 0)
        self.assertTrue(report_path.exists())
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
