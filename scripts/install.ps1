# J.A.R.V.I.S. Desk Pet — Windows 1-Liner Quick Installer
# Usage: iwr -useb https://raw.githubusercontent.com/harshil342/J.A.R.V.I.S-Desk-Pet/main/scripts/install.ps1 | iex

$ErrorActionPreference = 'Stop'
Write-Host "⚡ Installing J.A.R.V.I.S. Desk Pet (v0.11.0)..." -ForegroundColor Cyan

$Repo = "harshil342/J.A.R.V.I.S-Desk-Pet"
$Arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "arm64" }
$FileName = "Deskpet-0.11.0-$Arch.exe"
$Url = "https://github.com/$Repo/releases/download/v0.11.0/$FileName"
$Dest = "$env:TEMP\$FileName"

Write-Host "⬇️ Downloading installer from $Url..." -ForegroundColor DarkGray
Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing

Write-Host "🚀 Launching setup wizard..." -ForegroundColor Green
Start-Process -FilePath $Dest -Wait
Remove-Item -Path $Dest -Force -ErrorAction SilentlyContinue

Write-Host "✅ J.A.R.V.I.S. Desk Pet installation complete!" -ForegroundColor Cyan
