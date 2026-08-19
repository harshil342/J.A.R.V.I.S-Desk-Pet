$ErrorActionPreference = "SilentlyContinue"
Get-Process node, llama-server, python, pythonw -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -match 'Deskpet|clawd|llama|nodejs'
} | Stop-Process -Force
Start-Sleep -Seconds 2
Write-Host "app processes stopped"
