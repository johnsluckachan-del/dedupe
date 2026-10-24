"""Tests for Pass 3: Fuzzy-name duplicates."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from dedupe.core import TrashManager
from dedupe.fuzzy import (
    check_copy_marker,
    differ_only_in_digits,
    normalize_stem,
    run_fuzzy_pass,
    strip_copy_markers,
)


class TestFuzzyPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, data: str, mtime: float | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode())
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_copy_marker_detection_and_stripping(self):
        cases = [
            ("report (1)", True, "report"),
            ("report - Copy", True, "report"),
            ("report - copy (2)", True, "report"),
            ("Copy of budget", True, "budget"),
            ("file_copy3", True, "file"),
            ("chapter-1", False, "chapter-1"),
            ("IMG_0001", False, "IMG_0001"),
            ("report_v1", False, "report_v1"),
        ]
        for stem, has_marker, expected_stripped in cases:
            self.assertEqual(check_copy_marker(stem), has_marker, f"Failed for {stem}")
            self.assertEqual(strip_copy_markers(stem), expected_stripped, f"Failed for {stem}")

    def test_differ_only_in_digits(self):
        self.assertTrue(differ_only_in_digits("img 0001", "img 0002"))
        self.assertTrue(differ_only_in_digits("report v1", "report v2"))
        self.assertTrue(differ_only_in_digits("chapter 1", "chapter 2"))
        self.assertFalse(differ_only_in_digits("report", "report"))
        self.assertFalse(differ_only_in_digits("projectplan", "project plan"))

    def test_copy_marker_keeper_beats_newer_mtime(self):
        now = time.time()
        # Original is older, copy is newer -> original MUST survive
        orig = self._write("report.docx", "v1", mtime=now - 100)
        copy1 = self._write("report (1).docx", "v2", mtime=now)

        tm = TrashManager(self.root)
        removals, removed = run_fuzzy_pass(
            self.root, tm, removed_folders=set(), removed_files=set()
        )

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].kept_path, orig)
        self.assertEqual(removals[0].removed_path, copy1)
        self.assertEqual(removals[0].reason, "fuzzy")

    def test_similar_names_newest_mtime_keeper(self):
        now = time.time()
        # Neither has copy marker -> newest mtime wins
        old = self._write("projectplan.xlsx", "old", mtime=now - 1000)
        new = self._write("project plan.xlsx", "new", mtime=now)

        tm = TrashManager(self.root)
        removals, removed = run_fuzzy_pass(
            self.root, tm, removed_folders=set(), removed_files=set()
        )

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].kept_path, new)
        self.assertEqual(removals[0].removed_path, old)

    def test_different_extensions_not_compared(self):
        self._write("doc.txt", "text")
        self._write("doc.md", "markdown")

        tm = TrashManager(self.root)
        removals, removed = run_fuzzy_pass(
            self.root, tm, removed_folders=set(), removed_files=set()
        )

        self.assertEqual(len(removals), 0)

    def test_numbered_files_not_duplicates(self):
        self._write("IMG_0001.jpg", "a")
        self._write("IMG_0002.jpg", "b")
        self._write("report_v1.txt", "c")
        self._write("report_v2.txt", "d")

        tm = TrashManager(self.root)
        removals, removed = run_fuzzy_pass(
            self.root, tm, removed_folders=set(), removed_files=set()
        )

        self.assertEqual(len(removals), 0)


if __name__ == "__main__":
    unittest.main()
