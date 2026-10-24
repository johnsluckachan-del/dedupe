[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = (Get-Item $PSScriptRoot).Parent.FullName
$buildDir = Join-Path $repoRoot "build"
$distDir = Join-Path $repoRoot "dist"
$stageDir = Join-Path $buildDir "package"
$stageAppDir = Join-Path $stageDir "app\dedupe"
$zipPath = Join-Path $distDir "Dedupe-Setup.zip"

Write-Host "Cleaning build directory..."
if (Test-Path $buildDir) {
    Remove-Item -Path $buildDir -Recurse -Force
}
New-Item -ItemType Directory -Path $stageAppDir -Force | Out-Null
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

Write-Host "Copying app source..."
$sourceDir = Join-Path $repoRoot "dedupe"
$pyFiles = @(Get-ChildItem -Path $sourceDir -Filter "*.py" -File)
foreach ($f in $pyFiles) {
    Copy-Item -Path $f.FullName -Destination $stageAppDir -Force
}

Write-Host "Copying installer files..."
Copy-Item -Path (Join-Path $PSScriptRoot "Install.cmd") -Destination $stageDir -Force
Copy-Item -Path (Join-Path $PSScriptRoot "install.ps1") -Destination $stageDir -Force
Copy-Item -Path (Join-Path $PSScriptRoot "uninstall.ps1") -Destination $stageDir -Force
Copy-Item -Path (Join-Path $PSScriptRoot "README-INSTALL.txt") -Destination $stageDir -Force

if (Test-Path $zipPath) {
    Remove-Item -Path $zipPath -Force
}

Write-Host "Creating $zipPath..."
Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force

Write-Host "Built $zipPath"
exit 0
