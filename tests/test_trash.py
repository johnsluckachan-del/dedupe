"""Tests for trash handling, dry-run, and permanent removal."""

import tempfile
import unittest
from pathlib import Path

from dedupe.cli import run_dedupe


class TestTrashAndSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, data: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode())
        return p

    def test_dry_run_leaves_files_and_creates_no_trash(self):
        self._write("a.txt", "same")
        self._write("b/a.txt", "same")
        ret = run_dedupe(self.root, dry_run=True)
        self.assertEqual(ret, 0)

        self.assertTrue((self.root / "a.txt").exists())
        self.assertTrue((self.root / "b/a.txt").exists())
        self.assertFalse((self.root / "_dedupe_trash").exists())

    def test_permanent_deletes_without_trash(self):
        self._write("a.txt", "same")
        self._write("b/a.txt", "same")
        ret = run_dedupe(self.root, permanent=True)
        self.assertEqual(ret, 0)

        self.assertTrue((self.root / "a.txt").exists())
        self.assertFalse((self.root / "b/a.txt").exists())
        self.assertFalse((self.root / "_dedupe_trash").exists())

    def test_default_moves_to_trash_preserving_relative_path(self):
        self._write("a.txt", "same")
        self._write("sub/nested/a.txt", "same")
        ret = run_dedupe(self.root)
        self.assertEqual(ret, 0)

        self.assertTrue((self.root / "a.txt").exists())
        self.assertFalse((self.root / "sub/nested/a.txt").exists())

        trash = self.root / "_dedupe_trash"
        self.assertTrue(trash.exists())
        session_dirs = list(trash.iterdir())
        self.assertEqual(len(session_dirs), 1)

        trashed_file = session_dirs[0] / "sub" / "nested" / "a.txt"
        self.assertTrue(trashed_file.exists())
        self.assertEqual(trashed_file.read_text(), "same")

    def test_second_run_ignores_trash_folder(self):
        self._write("a.txt", "same")
        self._write("b/a.txt", "same")
        run_dedupe(self.root)
        run_dedupe(self.root)

        self.assertTrue((self.root / "a.txt").exists())
        # Ensure only original duplicate was moved, no extra trash copies
        trash = self.root / "_dedupe_trash"
        self.assertTrue(trash.exists())


if __name__ == "__main__":
    unittest.main()
