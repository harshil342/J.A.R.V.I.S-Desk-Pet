param([string]$Msg = "What time is it, Jarvis?")
$body = @{
  messages = @(@{ role = 'user'; content = $Msg })
  stream = $false
  max_new_tokens = 160
  temperature = 0.4
} | ConvertTo-Json -Depth 5
$r = Invoke-RestMethod -Uri http://127.0.0.1:18765/api/chat -Method Post -Body $body -ContentType 'application/json'
Write-Host "REPLY: $($r.content)"
