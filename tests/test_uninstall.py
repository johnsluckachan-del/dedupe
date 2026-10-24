"""Tests for packaging/uninstall.ps1 safety guards.

The uninstaller must delete a real Dedupe install folder, but must never
delete a foreign folder, the caller's current folder (empty $PSScriptRoot),
or a protected system folder. These tests invoke the real script with
powershell using throwaway shortcut dirs and -NoRegister so nothing outside
the temp dir is touched.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

UNINSTALL_PS1 = Path(__file__).resolve().parent.parent / "packaging" / "uninstall.ps1"

# The uninstaller schedules deletion through `cmd /c timeout 2 & rmdir`,
# so "kept" assertions must wait past that delay to be meaningful.
DELETE_DELAY_GRACE = 6.0
REMOVED_POLL_TIMEOUT = 30.0

POWERSHELL = shutil.which("powershell")


def _run_powershell(args, cwd=None, env=None):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        cwd=cwd,
        env=env,
    )


class TestUninstallScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.assertTrue(UNINSTALL_PS1.is_file(), "packaging/uninstall.ps1 missing")
        self.assertIsNotNone(
            POWERSHELL, "powershell is required to run packaging/uninstall.ps1"
        )

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except FileNotFoundError:
            pass

    def _shortcut_dirs(self):
        desktop = self.root / "fake-desktop"
        startmenu = self.root / "fake-startmenu"
        desktop.mkdir()
        startmenu.mkdir()
        (desktop / "Dedupe.lnk").write_text("fake shortcut")
        (startmenu / "Dedupe.lnk").write_text("fake shortcut")
        return desktop, startmenu

    def _shortcut_args(self, desktop, startmenu):
        return [
            "-ShortcutDir", str(desktop),
            "-StartMenuDir", str(startmenu),
            "-NoRegister",
        ]

    def _assert_shortcuts_removed(self, desktop, startmenu):
        self.assertFalse((desktop / "Dedupe.lnk").exists())
        self.assertFalse((startmenu / "Dedupe.lnk").exists())

    def _wait_for_removal(self, path):
        deadline = time.time() + REMOVED_POLL_TIMEOUT
        while time.time() < deadline:
            if not path.exists():
                return True
            time.sleep(0.5)
        return not path.exists()

    def _make_install_dir(self, name, with_markers=True):
        install = self.root / name
        if with_markers:
            (install / "dedupe").mkdir(parents=True)
            (install / "dedupe" / "cli.py").write_text("# fake cli")
            (install / "dedupe.cmd").write_text("@echo off")
        else:
            install.mkdir(parents=True)
        shutil.copy2(UNINSTALL_PS1, install / "uninstall.ps1")
        return install

    def test_real_install_folder_is_removed(self):
        install = self._make_install_dir("Dedupe", with_markers=True)
        desktop, startmenu = self._shortcut_dirs()

        proc = _run_powershell(
            ["-File", str(install / "uninstall.ps1")]
            + self._shortcut_args(desktop, startmenu)
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_shortcuts_removed(desktop, startmenu)
        self.assertTrue(
            self._wait_for_removal(install),
            "real Dedupe install folder was not removed",
        )

    def test_foreign_folder_is_kept(self):
        foreign = self._make_install_dir("SomeOtherApp", with_markers=False)
        desktop, startmenu = self._shortcut_dirs()

        proc = _run_powershell(
            ["-File", str(foreign / "uninstall.ps1")]
            + self._shortcut_args(desktop, startmenu)
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_shortcuts_removed(desktop, startmenu)
        self.assertIn("Warning", proc.stdout)
        time.sleep(DELETE_DELAY_GRACE)
        self.assertTrue(foreign.is_dir(), "foreign folder must never be deleted")
        self.assertTrue((foreign / "uninstall.ps1").is_file())

    def test_empty_psscriptroot_keeps_current_folder(self):
        victim = self.root / "victim-cwd"
        victim.mkdir()
        (victim / "keep.txt").write_text("must survive")
        desktop, startmenu = self._shortcut_dirs()
        # Invoke the script body as an anonymous script block so that
        # $PSScriptRoot is empty, mimicking the reported bug scenario.
        command = (
            "$sb = [scriptblock]::Create((Get-Content -LiteralPath "
            f"'{UNINSTALL_PS1}' -Raw)); "
            f"& $sb -ShortcutDir '{desktop}' -StartMenuDir '{startmenu}' -NoRegister"
        )

        proc = _run_powershell(["-Command", command], cwd=str(victim))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_shortcuts_removed(desktop, startmenu)
        self.assertIn("by hand", proc.stdout)
        time.sleep(DELETE_DELAY_GRACE)
        self.assertTrue(victim.is_dir(), "current folder must never be deleted")
        self.assertTrue((victim / "keep.txt").is_file())

    def test_protected_folder_is_kept(self):
        install = self._make_install_dir("Dedupe", with_markers=True)
        desktop, startmenu = self._shortcut_dirs()
        # Point %LOCALAPPDATA% at the install dir: the uninstaller must
        # refuse to delete a folder equal to a protected location even
        # though all install markers are present.
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(install)

        proc = _run_powershell(
            ["-File", str(install / "uninstall.ps1")]
            + self._shortcut_args(desktop, startmenu),
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("refusing", proc.stdout.lower())
        time.sleep(DELETE_DELAY_GRACE)
        self.assertTrue(install.is_dir(), "protected folder must never be deleted")


if __name__ == "__main__":
    unittest.main()
