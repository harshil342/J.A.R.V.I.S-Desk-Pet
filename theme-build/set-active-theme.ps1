param(
  [string]$ThemeId = "jarvis-arc"
)
$ErrorActionPreference = "Stop"

# Run only while the app is STOPPED (the app rewrites prefs on exit).
$p = Join-Path $env:APPDATA "deskpet\clawd-prefs.json"
$j = Get-Content $p -Raw | ConvertFrom-Json
$old = $j.theme
$j.theme = $ThemeId
[System.IO.File]::WriteAllText($p, ($j | ConvertTo-Json -Depth 30), (New-Object System.Text.UTF8Encoding $false))
Write-Output ("prefs theme: {0} -> {1}" -f $old, $ThemeId)
