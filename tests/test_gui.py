import sys
import unittest
from pathlib import Path
from unittest import mock

from dedupe.gui import child_python_executable, parse_gui_args


class TestParseGuiArgs(unittest.TestCase):
    def test_positional_folder(self):
        p = parse_gui_args([r"C:\data"])
        self.assertEqual(p["folder"], r"C:\data")
        self.assertFalse(p["smoke"])
        self.assertFalse(p["dry_run"])

    def test_smoke_args(self):
        p = parse_gui_args(["--smoke", r"C:\data", "--dry-run", "--no-similar", "--smoke-out", "out.txt"])
        self.assertTrue(p["smoke"])
        self.assertEqual(p["folder"], r"C:\data")
        self.assertTrue(p["dry_run"])
        self.assertTrue(p["no_similar"])
        self.assertEqual(p["smoke_out"], "out.txt")

    def test_defaults_no_similar_off_means_similar_on(self):
        p = parse_gui_args([])
        self.assertEqual(p["folder"], "")
        self.assertFalse(p["no_similar"])
        self.assertFalse(p["permanent"])

    def test_child_python_maps_pythonw_to_python(self):
        fake = str(Path("C:/Python312/pythonw.exe"))
        with mock.patch.object(sys, "executable", fake):
            # Path.is_file may be False on this machine layout; function returns candidate path anyway
            result = child_python_executable()
            self.assertTrue(result.lower().endswith("python.exe"))

    def test_child_python_keeps_python(self):
        fake = str(Path("C:/Python312/python.exe"))
        with mock.patch.object(sys, "executable", fake):
            result = child_python_executable()
            self.assertEqual(result, fake)


if __name__ == "__main__":
    unittest.main()
