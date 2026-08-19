$ErrorActionPreference = "Stop"

# Restore the original v1 Jarvis theme (backed up when Mako Class replaced it)
# as its own theme folder so the picker offers all three themes.
$src = "h:\apps\Deskpet\theme-build\backup-jarvis-v1"
$dest = Join-Path $env:APPDATA "deskpet\themes\jarvis-classic"

if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item $src $dest -Recurse

# Distinct display name so the three themes are tellable apart in the picker
$f = Join-Path $dest "theme.json"
$c = Get-Content $f -Raw
$c = $c.Replace('"name": "Jarvis Interface"', '"name": "Jarvis Classic"')
[System.IO.File]::WriteAllText($f, $c, (New-Object System.Text.UTF8Encoding $false))

$files = (Get-ChildItem $dest -Recurse -File).Count
Write-Output "Restored v1 theme as jarvis-classic ($files files)"
