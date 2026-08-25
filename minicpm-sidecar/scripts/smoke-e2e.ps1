<#
.SYNOPSIS
  DeskPet Jarvis end-to-end smoke test (Phase 6).

.DESCRIPTION
  Hits the running gateway on 127.0.0.1:18765 and verifies, in order:
     1. /api/health            - sidecar + llama-server alive, model loaded,
                                 watchdog fields (llama_restarts/degraded) present
     2. calculator tool        - canned deterministic math reply
     3. time tool              - canned clock reply with today's year
     4. todo lifecycle         - add an item, then clear the list (restores state)
     5. persona inference      - model answers in the Jarvis voice
     6. /api/models            - model discovery
     7. /api/tools             - tool catalog non-empty
     8. /api/adapters          - adapter listing answers
     9. memory roundtrip       - remember a fact, search it back, delete it
    10. task lifecycle         - schedule a reminder, list it, delete it
    11. MCP servers list       - answers (empty list is fine)
    12. chat SSE streaming     - stream=true yields parseable event frames
    13. tool_mode A/B          - explicit regex vs native both answer
  Every check prints PASS/FAIL with latency; exit code 0 only if all pass.
  All checks are offline-safe except the inference check (needs the model
  loaded, which is the normal running state).

.EXAMPLE
  .\scripts\smoke-e2e.ps1
  .\scripts\smoke-e2e.ps1 -BaseUrl "http://127.0.0.1:18765"
#>
param(
  [string]$BaseUrl = "http://127.0.0.1:18765",
  [int]$TimeoutSec = 120
)

$ErrorActionPreference = "Continue"
$script:failures = 0

function Chat([string]$msg) {
  $body = @{
    messages = @(@{ role = "user"; content = $msg })
    stream = $false
    max_new_tokens = 512
  } | ConvertTo-Json -Depth 5
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/chat" `
      -ContentType "application/json" -Body $body -TimeoutSec $TimeoutSec
    $sw.Stop()
    return [pscustomobject]@{ Ok = $true; Text = [string]$r.content; Ms = $sw.ElapsedMilliseconds }
  } catch {
    $sw.Stop()
    return [pscustomobject]@{ Ok = $false; Text = $_.Exception.Message; Ms = $sw.ElapsedMilliseconds }
  }
}

function Check([string]$name, [bool]$ok, [string]$detail, [long]$ms) {
  if ($ok) {
    Write-Output ("PASS  {0,-22} {1,6} ms  {2}" -f $name, $ms, $detail)
  } else {
    Write-Output ("FAIL  {0,-22} {1,6} ms  {2}" -f $name, $ms, $detail)
    $script:failures += 1
  }
}

Write-Output "DeskPet Jarvis E2E smoke against $BaseUrl"
Write-Output ("-" * 72)

# 1. Health â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $h = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 15
  $sw.Stop()
  $alive = ($h.ok -eq $true) -and ($h.alive -eq $true -or ($h.llama_server.status -eq "ok"))
  $model = [string]$h.model_name
  # Invoke-RestMethod collapses JSON nulls to $null, so verify key
  # presence against the raw body instead of the parsed object.
  $rawHealth = (Invoke-WebRequest -Uri "$BaseUrl/api/health" -TimeoutSec 15 -UseBasicParsing).Content
  $watchdog = ($rawHealth -match '"llama_restarts"') -and ($rawHealth -match '"degraded"')
  Check "health" $alive ("model=" + $model + " restarts=" + $h.llama_restarts + " degraded=" + $h.degraded) $sw.ElapsedMilliseconds
  Check "health watchdog fields" $watchdog "llama_restarts/degraded exposed by crash supervisor" $sw.ElapsedMilliseconds
  if (-not $alive) {
    Write-Output "Gateway unhealthy - aborting remaining checks."
    exit 1
  }
} catch {
  $sw.Stop()
  Check "health" $false $_.Exception.Message $sw.ElapsedMilliseconds
  Write-Output "Gateway unreachable - is the app running (npm start)?"
  exit 1
}

# 2. Calculator (canned, deterministic) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$r = Chat "what is 12 plus 7"
Check "tool: calculator" ($r.Ok -and ($r.Text -match "19")) $r.Text $r.Ms

# 3. Time (canned, deterministic) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$r = Chat "what time is it"
$year = (Get-Date).Year
Check "tool: time" ($r.Ok -and ($r.Text -match [string]$year)) $r.Text $r.Ms

# 4. Todo lifecycle (offline file; restores state via clear) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$r = Chat "add smoke test item to my todo"
$added = $r.Ok -and ($r.Text -match "smoke test item")
$r2 = Chat "clear my todo list"
$cleared = $r2.Ok -and ($r2.Text -match "(?i)clear|removed|empty")
Check "tool: todo add+clear" ($added -and $cleared) ("add: " + $r.Text + " | clear: " + $r2.Text) ($r.Ms + $r2.Ms)

# 5. Persona inference â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$r = Chat "who are you"
$persona = $r.Ok -and ($r.Text -match "(?i)jarvis|butler|assistant") -and ($r.Text.Length -gt 20)
Check "persona inference" $persona ($r.Text.Substring(0, [Math]::Min(80, $r.Text.Length)) + "...") $r.Ms

# 6. Model discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $m = Invoke-RestMethod -Uri "$BaseUrl/api/models" -TimeoutSec 15
  $sw.Stop()
  $count = @($m.items).Count
  Check "models list" ($count -ge 1) "$count model(s) discovered" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "models list" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 7. Tool catalog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $t = Invoke-RestMethod -Uri "$BaseUrl/api/tools" -TimeoutSec 15
  $sw.Stop()
  $names = @($t.tools | ForEach-Object { $_.name })
  Check "tools catalog" ($names.Count -ge 10) "$($names.Count) tools (incl. calculate=$($names -contains 'calculate'))" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "tools catalog" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 8. Adapters listing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $a = Invoke-RestMethod -Uri "$BaseUrl/api/adapters" -TimeoutSec 15
  $sw.Stop()
  $activeName = if ($a.current_name) { $a.current_name } else { "base" }
  Check "adapters list" ($null -ne $a.items) "@($activeName) active" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "adapters list" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 9. Memory roundtrip (remember â†’ search â†’ delete, restores state) â”€â”€â”€â”€â”€
$stamp = Get-Date -Format "yyyyMMddHHmmss"
$fact = "smoke-e2e fact: staging db port is $stamp"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $body = @{ text = $fact; category = "technical" } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/memory" -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
  $qbody = @{ query = "staging db port $stamp"; limit = 5 } | ConvertTo-Json
  $found = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/memory/search" -ContentType "application/json" -Body $qbody -TimeoutSec 15
  $hit = @($found.matches | Where-Object { $_.memory.content -match $stamp -or $_.memory.text -match $stamp }).Count -ge 1
  # cleanup regardless of search outcome
  $all = Invoke-RestMethod -Uri "$BaseUrl/api/memory" -TimeoutSec 15
  foreach ($item in @($all.memories | Where-Object { $_.content -match $stamp -or $_.text -match $stamp })) {
    Invoke-RestMethod -Method Delete -Uri "$BaseUrl/api/memory/$($item.id)" -TimeoutSec 15 | Out-Null
  }
  $sw.Stop()
  Check "memory roundtrip" $hit ("remember -> search -> delete ('...port $stamp')") $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "memory roundtrip" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 10. Task lifecycle (schedule â†’ list â†’ delete) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $body = @{ name = "smoke-e2e reminder"; delay_seconds = 3600; payload = "" } | ConvertTo-Json
  $task = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/tasks" -ContentType "application/json" -Body $body -TimeoutSec 15
  $tasks = Invoke-RestMethod -Uri "$BaseUrl/api/tasks" -TimeoutSec 15
  $tid = $task.task.id
  $listed = @($tasks.tasks | Where-Object { $_.id -eq $tid }).Count -eq 1
  Invoke-RestMethod -Method Delete -Uri "$BaseUrl/api/tasks/$tid" -TimeoutSec 15 | Out-Null
  $sw.Stop()
  Check "task lifecycle" ($listed -and $tid) "schedule -> list -> delete id=$tid" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "task lifecycle" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 11. MCP servers listing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $mc = Invoke-RestMethod -Uri "$BaseUrl/api/mcp/servers" -TimeoutSec 15
  $sw.Stop()
  $mcpCount = @($mc.servers).Count
  Check "mcp servers list" ($null -ne $mc) "$mcpCount server(s) configured" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "mcp servers list" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 12. Chat SSE streaming (stream=true must yield parseable frames) â”€â”€â”€â”€â”€â”€
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  Add-Type -AssemblyName System.Net.Http
  $client = [System.Net.Http.HttpClient]::new()
  $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
  $payload = @{
    messages = @(@{ role = "user"; content = "say hi in 3 words" })
    stream = $true
    max_new_tokens = 64
  } | ConvertTo-Json -Depth 5
  $content = [System.Net.Http.StringContent]::new($payload, [Text.Encoding]::UTF8, "application/json")
  $resp = $client.PostAsync("$BaseUrl/api/chat", $content).GetAwaiter().GetResult()
  $stream = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
  $reader = [IO.StreamReader]::new($stream)
  $frames = 0; $sawDelta = $false; $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline -and $frames -lt 50) {
    $line = $reader.ReadLine()
    if ($null -eq $line) { break }
    if (-not $line.StartsWith("data:")) { continue }
    $obj = $line.Substring(5).Trim() | ConvertFrom-Json
    $frames++
    if ($obj.event -eq "delta" -or $obj.content) { $sawDelta = $true; break }
  }
  $reader.Dispose(); $client.Dispose()
  $sw.Stop()
  Check "chat SSE stream" ($frames -ge 1 -and $sawDelta) "$frames frame(s), delta received" $sw.ElapsedMilliseconds
} catch {
  $sw.Stop(); Check "chat SSE stream" $false $_.Exception.Message $sw.ElapsedMilliseconds
}

# 13. tool_mode A/B â€” explicit regex vs native both produce replies â”€â”€â”€â”€â”€
foreach ($mode in @("regex", "native")) {
  $body = @{
    messages = @(@{ role = "user"; content = "what is 21 plus 21" })
    stream = $false
    max_new_tokens = 256
    tool_mode = $mode
  } | ConvertTo-Json -Depth 5
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/chat" `
      -ContentType "application/json" -Body $body -TimeoutSec $TimeoutSec
    $sw.Stop()
    $txt = [string]$r.content
    Check ("tool_mode={0}" -f $mode) ($txt -match "42") ($txt.Substring(0, [Math]::Min(60, $txt.Length))) $sw.ElapsedMilliseconds
  } catch {
    $sw.Stop(); Check ("tool_mode={0}" -f $mode) $false $_.Exception.Message $sw.ElapsedMilliseconds
  }
}

Write-Output ("-" * 72)
if ($script:failures -eq 0) {
  Write-Output "ALL CHECKS PASSED"
  exit 0
} else {
  Write-Output ("$($script:failures) CHECK(S) FAILED")
  exit 1
}
