[CmdletBinding()]
param(
    [string]$ShortcutDir = [Environment]::GetFolderPath("Desktop"),
    [string]$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    [switch]$NoRegister
)

$InstallDir = $PSScriptRoot
if (-not $InstallDir) {
    $InstallDir = (Get-Location).Path
}

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

if ($InstallDir -and (Test-Path $InstallDir)) {
    $tempDir = $env:TEMP
    if (-not $tempDir) { $tempDir = "C:\Windows\Temp" }
    Start-Process -FilePath "cmd.exe" -WorkingDirectory $tempDir -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$InstallDir`"" -WindowStyle Hidden
}

exit 0
