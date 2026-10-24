Dedupe - Install on Windows
============================

No administrator rights are needed. Nothing is sent off this PC.

1. Unzip Dedupe-Setup.zip to any folder.
2. Double-click Install.cmd.
   - It finds Python 3.10 or newer (or offers to install Python 3.12
     per-user with winget), copies the app, and creates shortcuts.
3. Use the "Dedupe" shortcut on your Desktop or in the Start Menu.
   - Double-click it and pick a folder, or drag a folder onto the
     shortcut to open Dedupe with that folder filled in.
   - You can also run dedupe.cmd from the install folder:
     %LOCALAPPDATA%\Programs\Dedupe\dedupe.cmd <folder>

Uninstall
---------
Open Settings > Apps and uninstall "Dedupe", or run uninstall.ps1
from the install folder:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\Dedupe\uninstall.ps1"

Uninstalling removes the app and its shortcuts. It leaves your
_dedupe_trash folders, the probe cache and gui.json settings alone.
