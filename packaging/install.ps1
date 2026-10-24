[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Dedupe",
    [string]$ShortcutDir = [Environment]::GetFolderPath("Desktop"),
    [string]$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    [switch]$NoFfmpeg,
    [switch]$NoRegister,
    [switch]$NoPip
)

$ErrorActionPreference = "Stop"

function Test-PythonCandidate {
    param([string]$ExePath)
    if (-not $ExePath) { return $null }
    if ($ExePath -like "*\WindowsApps\*") { return $null }
    if (-not (Test-Path $ExePath)) { return $null }
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $verRaw = & $ExePath -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null
        $code = $LASTEXITCODE
        $verOut = @($verRaw)[0]
        $ErrorActionPreference = $prev
        if ($code -ne 0 -or -not $verOut) { return $null }
        $parts = ([string]$verOut).Trim().Split('.')
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
            return $ExePath
        }
        return $null
    } catch {
        return $null
    }
}

function Find-Python {
    # 1) py launcher
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd -and $pyCmd.Source -notlike "*\WindowsApps\*") {
        try {
            $prev = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $exeRaw = & $pyCmd.Source -3 -c "import sys; print(sys.executable)" 2>$null
            $code = $LASTEXITCODE
            $exeOut = @($exeRaw)[0]
            $ErrorActionPreference = $prev
            if ($code -eq 0 -and $exeOut) {
                $found = Test-PythonCandidate -ExePath ([string]$exeOut).Trim()
                if ($found) { return $found }
            }
        } catch { }
    }
    # 2) python.exe on PATH, ignoring the WindowsApps store stub
    $pythonCmds = @(Get-Command python.exe -All -ErrorAction SilentlyContinue)
    if ($pythonCmds.Count -eq 0) {
        $pythonCmds = @(Get-Command python -All -ErrorAction SilentlyContinue)
    }
    foreach ($cmd in $pythonCmds) {
        if (-not $cmd.Source) { continue }
        if ($cmd.Source -like "*\WindowsApps\*") { continue }
        $found = Test-PythonCandidate -ExePath $cmd.Source
        if ($found) { return $found }
    }
    return $null
}

function Find-PythonInLocalAppData {
    $base = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (-not (Test-Path $base)) { return $null }
    $dirs = @(Get-ChildItem -Path $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
    foreach ($d in $dirs) {
        $candidate = Join-Path $d.FullName "python.exe"
        $found = Test-PythonCandidate -ExePath $candidate
        if ($found) { return $found }
    }
    return $null
}

$pythonExe = Find-Python
if (-not $pythonExe) {
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        Write-Host "Python not found. Installing Python with winget..."
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & winget install -e --id Python.Python.3.12 --scope user --silent
        $ErrorActionPreference = $prev
        $pythonExe = Find-Python
        if (-not $pythonExe) {
            $pythonExe = Find-PythonInLocalAppData
        }
    }
}
if (-not $pythonExe) {
    Write-Host "Python 3.10 or newer was not found on this PC."
    Write-Host "Please install Python 3.12 from https://www.python.org/downloads/ (per-user install is fine), then run Install.cmd again."
    Write-Host "Make sure to tick 'Add python.exe to PATH' in the Python installer if you install manually."
    exit 1
}

Write-Host "Using Python: $pythonExe"
$pythonDir = Split-Path $pythonExe -Parent
$pythonwExe = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    $pythonwExe = $pythonExe
}

# Locate the app payload next to this script (app\dedupe), or in the repo layout.
$payloadDir = Join-Path $PSScriptRoot "app\dedupe"
if (-not (Test-Path $payloadDir)) {
    $repoPayload = Join-Path (Split-Path $PSScriptRoot -Parent) "dedupe"
    if (Test-Path $repoPayload) {
        $payloadDir = $repoPayload
    } else {
        Write-Host "Could not find the app files (app\dedupe) next to install.ps1."
        exit 1
    }
}

# Copy app\dedupe to InstallDir\dedupe, replacing an older copy.
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
$destDedupe = Join-Path $InstallDir "dedupe"
if (Test-Path $destDedupe) {
    Remove-Item -Path $destDedupe -Recurse -Force
}
Copy-Item -Path $payloadDir -Destination $destDedupe -Recurse -Force

# Write dedupe.cmd launcher in InstallDir.
$dedupeCmd = Join-Path $InstallDir "dedupe.cmd"
$cmdLines = @(
    "@echo off",
    "setlocal",
    "set ""PYTHONPATH=$InstallDir""",
    """$pythonExe"" -m dedupe %*",
    "exit /b %ERRORLEVEL%"
)
Set-Content -Path $dedupeCmd -Value $cmdLines -Encoding ASCII

# Copy uninstall.ps1 into InstallDir.
$uninstallSource = Join-Path $PSScriptRoot "uninstall.ps1"
if (Test-Path $uninstallSource) {
    Copy-Item -Path $uninstallSource -Destination (Join-Path $InstallDir "uninstall.ps1") -Force
}

# Optional packages, unless -NoPip.
if (-not $NoPip) {
    $missing = @()
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $pythonExe -c "import PIL" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += "Pillow" }
    & $pythonExe -c "import pypdf" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += "pypdf" }
    $ErrorActionPreference = $prev
    if ($missing.Count -gt 0) {
        Write-Host "Installing optional packages: $($missing -join ', ')"
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $pythonExe -m pip install --user --upgrade @missing
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Warning: installing optional packages failed; image/PDF similarity may be skipped."
        }
        $ErrorActionPreference = $prev
    } else {
        Write-Host "Pillow and pypdf are already available."
    }
}

# Optional ffmpeg, unless -NoFfmpeg.
if (-not $NoFfmpeg) {
    $ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ffmpegCmd) {
        $ffmpegOk = $false
        $wingetCmd2 = Get-Command winget -ErrorAction SilentlyContinue
        if ($wingetCmd2) {
            $prev = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & winget install -e --id Gyan.FFmpeg --silent
            if ($LASTEXITCODE -eq 0) { $ffmpegOk = $true }
            $ErrorActionPreference = $prev
        }
        if (-not $ffmpegOk) {
            Write-Host "ffmpeg was not installed; video content matching will be skipped."
        }
    }
}

# Create shortcuts with WScript.Shell.
$wscript = New-Object -ComObject WScript.Shell
foreach ($dir in @($ShortcutDir, $StartMenuDir)) {
    if (-not $dir) { continue }
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $lnkPath = Join-Path $dir "Dedupe.lnk"
    $shortcut = $wscript.CreateShortcut($lnkPath)
    $shortcut.TargetPath = $pythonwExe
    $shortcut.Arguments = "-m dedupe.gui"
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "Dedupe - find and remove duplicate files"
    $shortcut.IconLocation = "$pythonwExe,0"
    $shortcut.Save()
}

# Register in HKCU uninstall list, unless -NoRegister.
if (-not $NoRegister) {
    $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Dedupe"
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }
    Set-ItemProperty -Path $regPath -Name "DisplayName" -Value "Dedupe"
    Set-ItemProperty -Path $regPath -Name "DisplayVersion" -Value "0.1.0"
    Set-ItemProperty -Path $regPath -Name "Publisher" -Value "V"
    $uninstallPs1 = Join-Path $InstallDir "uninstall.ps1"
    Set-ItemProperty -Path $regPath -Name "UninstallString" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallPs1`""
    Set-ItemProperty -Path $regPath -Name "InstallLocation" -Value $InstallDir
}

Write-Host "Dedupe installed to $InstallDir"
Write-Host "Desktop shortcut created. Double-click Dedupe, or drag a folder onto the shortcut."
exit 0
