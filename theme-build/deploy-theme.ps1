param(
  [string]$ThemeId = "jarvis"
)
$ErrorActionPreference = "Stop"

$stage = "h:\apps\Deskpet\theme-build\$ThemeId"
$dest = Join-Path $env:APPDATA "deskpet\themes\$ThemeId"

# 1. Validate staging completeness
$manifest = Get-Content (Join-Path $stage "theme.json") -Raw | ConvertFrom-Json
$referenced = @()
$manifest.states.PSObject.Properties.Value | ForEach-Object { $referenced += $_ }
$manifest.workingTiers | ForEach-Object { $referenced += $_.file }
$referenced += $manifest.reactions.drag.file
$referenced += $manifest.reactions.clickLeft.file
$referenced += $manifest.reactions.double.files
$referenced += $manifest.reactions.annoyed.file
$referenced += $manifest.sleepingHitboxFiles
$referenced = $referenced | Sort-Object -Unique

$missing = @()
foreach ($f in $referenced) {
  if (-not (Test-Path (Join-Path $stage "assets\$f"))) { $missing += $f }
}
if ($missing.Count -gt 0) {
  Write-Output "MISSING FILES IN STAGING:"
  $missing | ForEach-Object { Write-Output "  $_" }
  exit 1
}
Write-Output ("Staging OK: theme.json + {0} referenced assets (all present)" -f $referenced.Count)

# 2. Quick SVG sanity check: every svg has viewBox 200 and closing tag
$bad = @()
Get-ChildItem (Join-Path $stage "assets") -Filter *.svg | ForEach-Object {
  $c = Get-Content $_.FullName -Raw
  if ($c -notmatch 'viewBox="0 0 200 200"' -or $c -notmatch '</svg>') { $bad += $_.Name }
}
if ($bad.Count -gt 0) {
  Write-Output "MALFORMED SVGs:"
  $bad | ForEach-Object { Write-Output "  $_" }
  exit 1
}
Write-Output "SVG sanity check passed"

# 3. Replace the live theme (backup first, only if something already lives there)
$bak = "h:\apps\Deskpet\theme-build\backup-$ThemeId-prev"
if (Test-Path $dest) {
  if (Test-Path $bak) { Remove-Item $bak -Recurse -Force }
  Copy-Item $dest $bak -Recurse
  Write-Output "Old theme backed up to $bak"
  Remove-Item $dest -Recurse -Force
}
New-Item -ItemType Directory -Path $dest -Force | Out-Null
Copy-Item (Join-Path $stage "theme.json") $dest
New-Item -ItemType Directory -Path (Join-Path $dest "assets") -Force | Out-Null
Copy-Item (Join-Path $stage "assets\*.svg") (Join-Path $dest "assets")

$deployed = (Get-ChildItem $dest -Recurse -File).Count
Write-Output "Deployed $deployed files to $dest"
Write-Output "DEPLOY OK"
