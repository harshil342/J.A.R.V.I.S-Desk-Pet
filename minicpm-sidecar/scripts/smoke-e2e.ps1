<#
.SYNOPSIS
  DeskPet Jarvis end-to-end smoke test (Phase 6).

.DESCRIPTION
  Hits the running gateway on 127.0.0.1:18765 and verifies, in order:
    1. /api/health            - sidecar + llama-server alive, model loaded
    2. calculator tool        - canned deterministic math reply
    3. time tool              - canned clock reply with today's year
    4. todo lifecycle         - add an item, then clear the list (restores state)
    5. persona inference      - model answers in the Jarvis voice
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

# 1. Health ─────────────────────────────────────────────────────────────
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $h = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 15
  $sw.Stop()
  $alive = ($h.ok -eq $true) -and ($h.alive -eq $true -or ($h.llama_server.status -eq "ok"))
  $model = [string]$h.model_name
  Check "health" $alive ("model=" + $model) $sw.ElapsedMilliseconds
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

# 2. Calculator (canned, deterministic) ─────────────────────────────────
$r = Chat "what is 12 plus 7"
Check "tool: calculator" ($r.Ok -and ($r.Text -match "19")) $r.Text $r.Ms

# 3. Time (canned, deterministic) ───────────────────────────────────────
$r = Chat "what time is it"
$year = (Get-Date).Year
Check "tool: time" ($r.Ok -and ($r.Text -match [string]$year)) $r.Text $r.Ms

# 4. Todo lifecycle (offline file; restores state via clear) ────────────
$r = Chat "add smoke test item to my todo"
$added = $r.Ok -and ($r.Text -match "smoke test item")
$r2 = Chat "clear my todo list"
$cleared = $r2.Ok -and ($r2.Text -match "(?i)clear|removed|empty")
Check "tool: todo add+clear" ($added -and $cleared) ("add: " + $r.Text + " | clear: " + $r2.Text) ($r.Ms + $r2.Ms)

# 5. Persona inference ──────────────────────────────────────────────────
$r = Chat "who are you"
$persona = $r.Ok -and ($r.Text -match "(?i)jarvis|butler|assistant") -and ($r.Text.Length -gt 20)
Check "persona inference" $persona ($r.Text.Substring(0, [Math]::Min(80, $r.Text.Length)) + "...") $r.Ms

Write-Output ("-" * 72)
if ($script:failures -eq 0) {
  Write-Output "ALL CHECKS PASSED"
  exit 0
} else {
  Write-Output ("$($script:failures) CHECK(S) FAILED")
  exit 1
}
