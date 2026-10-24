"""Tests for Pass 1: Duplicate folders."""

import tempfile
import unittest
from pathlib import Path

from dedupe.core import TrashManager
from dedupe.folder import run_folder_pass


class TestFolderPass(unittest.TestCase):
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

    def test_duplicate_folders_keeper_shortest_name(self):
        # photos vs photos_backup
        for d in ("photos", "photos_backup"):
            self._write(f"{d}/1.jpg", "pic1")
            self._write(f"{d}/2.jpg", "pic2")

        tm = TrashManager(self.root)
        removals, removed = run_folder_pass(self.root, tm)

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].removed_path, self.root / "photos_backup")
        self.assertEqual(removals[0].kept_path, self.root / "photos")
        self.assertEqual(removals[0].reason, "folder")
        self.assertFalse((self.root / "photos_backup").exists())
        self.assertTrue((self.root / "photos").exists())

    def test_top_most_matching_folders_only(self):
        # Parent A and B identical; sub-folders inside should not be reported separately
        for parent in ("dirA", "dirB"):
            self._write(f"{parent}/root.txt", "root")
            self._write(f"{parent}/sub/inner.txt", "inner")

        tm = TrashManager(self.root)
        removals, removed = run_folder_pass(self.root, tm)

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].removed_path, self.root / "dirB")
        self.assertEqual(removals[0].kept_path, self.root / "dirA")
        self.assertIn(self.root / "dirB", removed)

    def test_empty_folders_ignored(self):
        # Folders with only 0-byte files must not be treated as duplicate folders
        self._write("emptyA/.keep", "")
        self._write("emptyB/.keep", "")

        tm = TrashManager(self.root)
        removals, removed = run_folder_pass(self.root, tm)

        self.assertEqual(len(removals), 0)
        self.assertTrue((self.root / "emptyA" / ".keep").exists())
        self.assertTrue((self.root / "emptyB" / ".keep").exists())

    def test_shallowest_depth_keeper(self):
        # depth 1 folder vs depth 2 folder with same contents
        for d in ("shallow", "nested/deep"):
            self._write(f"{d}/data.bin", "same_data")

        tm = TrashManager(self.root)
        removals, removed = run_folder_pass(self.root, tm)

        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].kept_path, self.root / "shallow")
        self.assertEqual(removals[0].removed_path, self.root / "nested" / "deep")


if __name__ == "__main__":
    unittest.main()
