[CmdletBinding()]
param(
    [string]$ShortcutDir = [Environment]::GetFolderPath("Desktop"),
    [string]$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    [switch]$NoRegister
)

$InstallDir = $PSScriptRoot

foreach ($dir in @($ShortcutDir, $StartMenuDir)) {
    if ($dir -and (Test-Path $dir)) {
        $lnk = Join-Path $dir "Dedupe.lnk"
        if (Test-Path $lnk) {
            Remove-Item -Path $lnk -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $NoRegister) {
    $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Dedupe"
    if (Test-Path $regPath) {
        Remove-Item -Path $regPath -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Dedupe uninstalled. Your scan results, trash folders and settings were left alone."

function Get-NormalizedPath {
    param([string]$Path)
    if (-not $Path) { return $null }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
    } catch {
        $full = $Path
    }
    return $full.TrimEnd('\', '/')
}

$deleteDir = $null
if (-not $InstallDir) {
    Write-Host "Could not determine the install folder automatically; please remove the app folder by hand."
} else {
    try {
        $installFull = [System.IO.Path]::GetFullPath($InstallDir)
    } catch {
        $installFull = $InstallDir
    }
    $normalized = $installFull.TrimEnd('\', '/')
    $protected = @()
    try {
        $driveRoot = [System.IO.Path]::GetPathRoot($installFull)
        if ($driveRoot) { $protected += $driveRoot }
    } catch { }
    $protected += @(
        $env:USERPROFILE,
        [Environment]::GetFolderPath("Desktop"),
        [Environment]::GetFolderPath("MyDocuments"),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:SystemRoot,
        $env:LOCALAPPDATA,
        $env:APPDATA
    )
    $isProtected = $false
    foreach ($p in $protected) {
        $np = Get-NormalizedPath -Path $p
        if ($np -and ($normalized -ieq $np)) { $isProtected = $true; break }
    }
    if ($isProtected) {
        Write-Host "Warning: refusing to delete protected folder '$InstallDir'; please remove it by hand if needed."
    } elseif (-not ((Test-Path -LiteralPath (Join-Path $InstallDir "dedupe\cli.py") -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $InstallDir "dedupe.cmd") -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $InstallDir "uninstall.ps1") -PathType Leaf))) {
        Write-Host "Warning: '$InstallDir' does not look like a Dedupe install (missing dedupe\cli.py, dedupe.cmd or uninstall.ps1); leaving it alone."
    } else {
        $deleteDir = $InstallDir
    }
}

if ($deleteDir -and (Test-Path $deleteDir)) {
    $tempDir = $env:TEMP
    if (-not $tempDir) { $tempDir = "C:\Windows\Temp" }
    Start-Process -FilePath "cmd.exe" -WorkingDirectory $tempDir -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$deleteDir`"" -WindowStyle Hidden
}

exit 0
