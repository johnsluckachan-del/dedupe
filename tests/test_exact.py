"""Tests for Pass 2: Exact duplicate files."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from dedupe.core import TrashManager
from dedupe.exact import run_exact_pass


class TestExactPass(unittest.TestCase):
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

    def test_exact_duplicates_across_tree(self):
        p1 = self._write("a.txt", "content123")
        p2 = self._write("sub/a_copy.txt", "content123")
        p3 = self._write("unique.txt", "different")

        tm = TrashManager(self.root)
        removals, removed = run_exact_pass(self.root, tm, removed_folders=set())

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].kept_path, p1)
        self.assertEqual(removals[0].removed_path, p2)
        self.assertEqual(removals[0].reason, "exact")

    def test_zero_byte_files_skipped(self):
        self._write("empty1.txt", "")
        self._write("empty2.txt", "")

        tm = TrashManager(self.root)
        removals, removed = run_exact_pass(self.root, tm, removed_folders=set())

        self.assertEqual(len(removals), 0)
        self.assertTrue((self.root / "empty1.txt").exists())
        self.assertTrue((self.root / "empty2.txt").exists())

    def test_keeper_oldest_mtime_tiebreak(self):
        now = time.time()
        # Same depth, same name length
        p_old = self._write("sub1/f.txt", "identical", mtime=now - 500)
        p_new = self._write("sub2/f.txt", "identical", mtime=now)

        tm = TrashManager(self.root)
        removals, removed = run_exact_pass(self.root, tm, removed_folders=set())

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].kept_path, p_old)
        self.assertEqual(removals[0].removed_path, p_new)


if __name__ == "__main__":
    unittest.main()
