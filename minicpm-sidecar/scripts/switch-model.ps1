$ErrorActionPreference = "Stop"

# Point DeskPet at the stock MiniCPM5 base model (Q8_0 ~ FP16 quality at 1B).
# Run only while the app is STOPPED (the app rewrites prefs on exit).
$p = Join-Path $env:APPDATA "deskpet\minicpm-prefs.json"
$j = if (Test-Path $p) { Get-Content $p -Raw | ConvertFrom-Json } else { [PSCustomObject]@{} }
$old = $j.model_dir
$j | Add-Member -NotePropertyName model_dir -NotePropertyValue "H:\LLMS\MiniCPM5" -Force
[System.IO.File]::WriteAllText($p, ($j | ConvertTo-Json -Depth 30), (New-Object System.Text.UTF8Encoding $false))
Write-Output ("model_dir: {0} -> {1}" -f $old, "H:\LLMS\MiniCPM5")
