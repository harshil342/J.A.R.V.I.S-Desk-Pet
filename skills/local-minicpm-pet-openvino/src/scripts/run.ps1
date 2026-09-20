$ErrorActionPreference = 'Stop'

# ── local-minicpm-pet-openvino deploy script ──
# One-click deploy of MiniCPM desk pet + OpenVINO inference backend.
# Flow: hardware check -> mirror config -> Python env -> pet source -> npm install ->
#       onboarding sentinel -> start OpenVINO inference service -> start pet frontend
#
# Args:
#   --china    pin mainland-China mirrors (GitCode/Tsinghua/Taobao/npmmirror)
#
# !! All large-file ops must run outside the sandbox (on the host filesystem) !!

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillRoot = Split-Path -Parent $ScriptDir

# ── Persistent root dirs ──
$OpenVinoRoot = Join-Path $env:USERPROFILE ".openvino"
$VenvRoot = Join-Path $OpenVinoRoot "venv"
$ModelsRoot = Join-Path $OpenVinoRoot "models"
$LogRoot = Join-Path $OpenVinoRoot "log"

foreach ($dir in @($OpenVinoRoot, $VenvRoot, $ModelsRoot, $LogRoot)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

# ── Parse args ──
$China = $false
$Stop = $false
$Status = $false
$Debug = $false
$Device = ""

for ($i = 0; $i -lt $args.Count; $i++) {
    switch ($args[$i]) {
        "--china"  { $China = $true }
        "--stop"   { $Stop = $true }
        "--status" { $Status = $true }
        "--debug"  { $Debug = $true }
        "--device" {
            $i++
            if ($i -lt $args.Count) { $Device = $args[$i].ToUpper() }
        }
        default {
            Write-Host "Unknown arg: $($args[$i])"
            Write-Host ""
            Write-Host "Usage: scripts\run.ps1 [--china] [--device NPU|GPU|CPU]  deploy and start"
            Write-Host "      scripts\run.ps1 --status                          show status"
            Write-Host "      scripts\run.ps1 --stop                            stop all services"
            Write-Host "      scripts\run.ps1 --debug                           print diagnostics"
            Write-Host ""
            Write-Host "  --china          use mainland-China mirrors"
            Write-Host "  --device <DEV>   inference device: NPU, GPU, CPU (default auto-detect)"
            exit 1
        }
    }
}

# Set inference device env var (read by server.py)
if ($Device) {
    if ($Device -notin @("NPU", "GPU", "CPU")) {
        Write-Host "Error: --device must be NPU, GPU, or CPU"
        exit 1
    }
    $env:OPENVINO_DEVICE = $Device
}

# ── --status: show runtime status ──
if ($Status) {
    Write-Host "=============================================="
    Write-Host " MiniCPM desk pet env status"
    Write-Host "=============================================="
    Write-Host ""

    # Check inference service
    $serverUp = $false
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            $health = $resp.Content | ConvertFrom-Json
            $serverUp = $true
            Write-Host "  inference service: running (status=$($health.status), uptime=$($health.uptime_s)s)"
        }
    } catch {}
    if (-not $serverUp) {
        Write-Host "  inference service: not running"
    }

    # Check pet frontend
    $petUp = $false
    try {
        $procs = Get-Process -Name "electron", "MiniCPM*", "Clawd*" -ErrorAction SilentlyContinue
        if ($procs) { $petUp = $true }
    } catch {}
    if ($petUp) {
        Write-Host "  pet frontend: running"
    } else {
        Write-Host "  pet frontend: not running"
    }

    Write-Host ""
    exit 0
}

# ── --debug: print diagnostics ──
if ($Debug) {
    Write-Host "=============================================="
    Write-Host " MiniCPM desk pet diagnostics"
    Write-Host "=============================================="
    Write-Host ""

    # 1. System info
    Write-Host "[System]"
    Write-Host "  OS: $([System.Environment]::OSVersion.VersionString)"
    Write-Host "  Platform: $env:PROCESSOR_ARCHITECTURE"
    Write-Host ""

    # 2. Python env
    Write-Host "[Python env]"
    $InfoJson = Get-Content (Join-Path $SkillRoot "info.json") -ErrorAction SilentlyContinue | ConvertFrom-Json
    $VenvName = $InfoJson.venv_name
    $VenvDir = Join-Path $VenvRoot $VenvName
    $Python = Join-Path $VenvDir "Scripts\python.exe"
    if (Test-Path $Python) {
        Write-Host "  venv: $VenvDir (exists)"
        $pyVer = & $Python --version 2>&1
        Write-Host "  Python version: $pyVer"
        Write-Host "  OpenVINO packages:"
        & (Join-Path $VenvDir "Scripts\pip.exe") list 2>$null | Select-String -Pattern "openvino|fastapi|uvicorn|modelscope" | ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "  venv: $VenvDir (missing)"
    }
    Write-Host ""

    # 3. Model dir
    Write-Host "[Model]"
    $ModelDir = Join-Path $ModelsRoot $InfoJson.models[0].dir_name
    if (Test-Path $ModelDir) {
        Write-Host "  dir: $ModelDir (exists)"
        $files = Get-ChildItem $ModelDir -ErrorAction SilentlyContinue
        Write-Host "  file count: $($files.Count)"
        $files | Select-Object Name, Length | ForEach-Object { Write-Host "    $($_.Name) ($([math]::Round($_.Length/1MB, 1)) MB)" }
    } else {
        Write-Host "  dir: $ModelDir (missing)"
    }
    Write-Host ""

    # 4. Inference service status
    Write-Host "[Inference service]"
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            Write-Host "  status: running"
            Write-Host "  response: $($resp.Content)"
        }
    } catch {
        Write-Host "  status: not running or unreachable"
    }

    # Check port usage
    Write-Host "  port 18765 usage:"
    $portInfo = netstat -ano 2>$null | Select-String ":18765"
    if ($portInfo) {
        $portInfo | ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "    free"
    }
    Write-Host ""

    # 5. Pet frontend
    Write-Host "[Pet frontend]"
    $petProcs = Get-Process -Name "electron", "MiniCPM*", "Clawd*" -ErrorAction SilentlyContinue
    if ($petProcs) {
        $petProcs | ForEach-Object { Write-Host "  process: $($_.ProcessName) (PID=$($_.Id))" }
    } else {
        Write-Host "  status: not running"
    }
    Write-Host ""

    # 6. Env vars
    Write-Host "[Env vars]"
    Write-Host "  MINICPM_BACKEND=$env:MINICPM_BACKEND"
    Write-Host "  PIP_INDEX_URL=$env:PIP_INDEX_URL"
    Write-Host "  ELECTRON_MIRROR=$env:ELECTRON_MIRROR"
    Write-Host "  HF_ENDPOINT=$env:HF_ENDPOINT"
    Write-Host ""

    # 7. Recent logs
    Write-Host "[Recent logs (last 20 lines)]"
    $latestLog = Get-ChildItem $LogRoot -Filter "*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latestLog) {
        Write-Host "  file: $($latestLog.FullName)"
        Get-Content $latestLog.FullName -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "  no log files"
    }

    Write-Host ""
    exit 0
}

# ── --stop: stop all services ──
if ($Stop) {
    Write-Host "Stopping MiniCPM desk pet env..."
    Write-Host ""

    # 1. Send graceful shutdown signal
    $serverStopped = $false
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/shutdown" -Method POST -TimeoutSec 5 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            $serverStopped = $true
            Write-Host "  inference service: stop signal sent"
        }
    } catch {}
    if (-not $serverStopped) {
        Write-Host "  inference service: not running (or already stopped)"
    }

    # 2. Wait for graceful exit
    if ($serverStopped) {
        Start-Sleep -Seconds 3
    }

    # 3. Force-clean leftover Python server processes
    try {
        $pyProcs = Get-Process -Name "python", "python3", "pythonw" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "server\.py" }
        if ($pyProcs) {
            $pyProcs | Stop-Process -Force
            Write-Host "  inference service: leftover processes killed"
        }
    } catch {}

    # 4. Confirm port release, kill holder by PID if needed
    $portInUse = netstat -ano 2>$null | Select-String ":18765.*LISTEN"
    if ($portInUse) {
        $pidMatch = $portInUse.ToString() -match '\s(\d+)\s*$'
        if ($pidMatch) {
            $orphanPid = [int]$Matches[1]
            Write-Host "  port 18765 still held by PID $orphanPid, killing..."
            Stop-Process -Id $orphanPid -Force -ErrorAction SilentlyContinue
        }
    }

    # 5. Stop pet frontend
    try {
        $procs = Get-Process -Name "electron", "MiniCPM*", "Clawd*" -ErrorAction SilentlyContinue
        if ($procs) {
            $procs | Stop-Process -Force
            Write-Host "  pet frontend: stopped"
        } else {
            Write-Host "  pet frontend: not running"
        }
    } catch {
        Write-Host "  pet frontend: stop failed ($_)"
    }

    Write-Host ""
    Write-Host "All stopped."
    exit 0
}

# ── Step 1: hardware check ──
Write-Host "=============================================="
Write-Host " MiniCPM desk pet + OpenVINO backend deploy tool"
Write-Host "=============================================="
Write-Host ""

Write-Host "[Step 1] Detecting CPU..."

$cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue
$cpuName = if ($cpu) { $cpu.Name.Trim() } else { "Unknown" }
$manufacturer = if ($cpu) { $cpu.Manufacturer } else { "Unknown" }

Write-Host "  CPU: $cpuName"
Write-Host "  vendor: $manufacturer"

# Brand check: block non-Intel CPUs
if ($manufacturer -ne "GenuineIntel") {
    Write-Host ""
    Write-Host "=============================================="
    Write-Host "  Error: non-Intel CPU detected ($manufacturer)"
    Write-Host ""
    Write-Host "  OpenVINO acceleration requires an Intel CPU."
    Write-Host "  Your CPU: $cpuName"
    Write-Host ""
    Write-Host "  To use the MiniCPM desk pet, consider:"
    Write-Host "    - Using the default llama.cpp backend (supports more platforms)"
    Write-Host "    - Or switching to an Intel Core Ultra PC"
    Write-Host "=============================================="
    exit 1
}

# Model check: warn but don't block when not on the known-accelerated list
$supportedPatterns = @("Core.*Ultra", "Xeon", "Arc", "Core.*1[2-4]\d{2,3}")
$isKnownSupported = $false
foreach ($pat in $supportedPatterns) {
    if ($cpuName -match $pat) {
        $isKnownSupported = $true
        break
    }
}

if (-not $isKnownSupported) {
    Write-Host ""
    Write-Host "  Warning: your Intel CPU is not on the known best-support list."
    Write-Host "  Inference still runs (CPU mode), but may be slow."
    Write-Host "  Recommended: Intel Core Ultra (Lunar Lake/Meteor Lake/Arrow Lake)"
    Write-Host "  Continuing deploy... (Ctrl+C to cancel)"
    Start-Sleep -Seconds 3
} else {
    Write-Host "  Hardware check passed."
}

# Extra platform.exe check (if present)
$PlatformExe = Join-Path $SkillRoot "bin\platform.exe"
if (Test-Path $PlatformExe) {
    $hasNpu = & $PlatformExe --has-npu 2>$null
    $hasGpu = & $PlatformExe --has-gpu 2>$null
    Write-Host "  NPU: $(if($hasNpu -eq '1'){'available'}else{'unavailable'})"
    Write-Host "  GPU: $(if($hasGpu -eq '1'){'available'}else{'unavailable'})"
}

# ── Step 2: mirror config ──
if ($China) {
    Write-Host ""
    Write-Host "[Step 2] Mainland-China mirror mode enabled (--china)"
    $env:PIP_INDEX_URL = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
    $env:PIP_TRUSTED_HOST = "mirrors.tuna.tsinghua.edu.cn"
    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
    $env:HF_ENDPOINT = "https://hf-mirror.com"
} else {
    Write-Host ""
    Write-Host "[Step 2] Using default sources (direct-access env)"
}

# ── Step 3: Python env + inference deps ──
Write-Host ""
$InfoJson = Get-Content (Join-Path $SkillRoot "info.json") | ConvertFrom-Json
$VenvName = $InfoJson.venv_name
$VenvDir = Join-Path $VenvRoot $VenvName
$Python = Join-Path $VenvDir "Scripts\python.exe"
$Pip = Join-Path $VenvDir "Scripts\pip.exe"

if (-not (Test-Path $Python)) {
    Write-Host "[Step 3] Creating Python venv: $VenvDir ..."
    & python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: failed to create venv. Ensure Python 3.11+ is installed."
        Write-Host "Check: python --version"
        exit 1
    }
} else {
    Write-Host "[Step 3] Python venv exists, skipping create."
}

$RequirementsFile = Join-Path $SkillRoot "requirements.txt"
if (Test-Path $RequirementsFile) {
    Write-Host "[Step 3] Installing Python deps..."
    if ($China) {
        & $Pip install -i $env:PIP_INDEX_URL --trusted-host $env:PIP_TRUSTED_HOST -r $RequirementsFile -q
    } else {
        & $Pip install -r $RequirementsFile -q
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: some deps failed to install, continuing..."
    } else {
        Write-Host "Python deps installed."
    }
}

# ── Step 4: fetch pet source ──
Write-Host ""
$PetDir = Join-Path $SkillRoot "..\..\clawd-on-desk"
$PetDir = [System.IO.Path]::GetFullPath($PetDir)
$PetRepoUrl_GitCode = "https://gitcode.com/OpenBMB/MiniCPM-Desk-Pet.git"
$PetRepoUrl_GitHub = "https://github.com/OpenBMB/MiniCPM-Desk-Pet.git"

if (-not (Test-Path (Join-Path $PetDir "package.json"))) {
    Write-Host "[Step 4] Pet source missing locally, fetching..."

    # Skip Git LFS large-file download (model managed separately by server.py)
    $env:GIT_LFS_SKIP_SMUDGE = "1"

    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCmd) {
        Write-Host "Error: git not found. Install git first."
        exit 1
    }

    $ParentDir = Split-Path -Parent $PetDir
    if (-not (Test-Path $ParentDir)) {
        New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
    }

    Push-Location $ParentDir

    if ($China) {
        Write-Host "git clone --depth 1 $PetRepoUrl_GitCode (CN mirror)..."
        & git clone --depth 1 $PetRepoUrl_GitCode "clawd-on-desk-repo"
        if ($LASTEXITCODE -ne 0) {
            Pop-Location
            Write-Host "Error: GitCode clone failed. Check network connection."
            exit 1
        }
    } else {
        Write-Host "git clone --depth 1 $PetRepoUrl_GitHub ..."
        & git clone --depth 1 $PetRepoUrl_GitHub "clawd-on-desk-repo" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "GitHub unreachable, trying GitCode CN mirror..."
            & git clone --depth 1 $PetRepoUrl_GitCode "clawd-on-desk-repo"
        }
        if ($LASTEXITCODE -ne 0) {
            Pop-Location
            Write-Host "Error: git clone failed (both GitHub and GitCode unreachable)."
            exit 1
        }
    }

    if (Test-Path "clawd-on-desk-repo\clawd-on-desk") {
        Move-Item "clawd-on-desk-repo\clawd-on-desk" "clawd-on-desk" -Force
        Remove-Item "clawd-on-desk-repo" -Recurse -Force
    } else {
        Rename-Item "clawd-on-desk-repo" "clawd-on-desk"
    }
    Pop-Location
    Write-Host "Pet source fetched."
} else {
    Write-Host "[Step 4] Pet source exists, skipping fetch."
}

# ── Step 5: install pet npm deps ──
Write-Host ""
if (-not (Test-Path (Join-Path $PetDir "node_modules"))) {
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        Write-Host "Error: npm not found. Install Node.js 18+ first."
        exit 1
    }

    Push-Location $PetDir
    if ($China) {
        Write-Host "[Step 5] Installing pet npm deps (Taobao mirror)..."
        & npm config set registry https://registry.npmmirror.com
    } else {
        Write-Host "[Step 5] Installing pet npm deps..."
    }
    & npm install
    Pop-Location
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: npm install may not have fully succeeded, continuing..."
    } else {
        Write-Host "Pet npm deps installed."
    }
} else {
    Write-Host "[Step 5] node_modules exists, skipping npm install."
}

# ── Step 6: pre-write onboarding sentinel (skip wizard) ──
Write-Host ""
$UserDataDir = Join-Path $env:APPDATA "Clawd on Desk"
if (-not (Test-Path $UserDataDir)) {
    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
}

$SentinelFile = Join-Path $UserDataDir "minicpm-onboarding.json"
if (-not (Test-Path $SentinelFile)) {
    Write-Host "[Step 6] Writing onboarding sentinel (skip wizard)..."
    $sentinel = @{
        complete = $true
        version = 1
        ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
        source = "local-minicpm-pet-openvino skill"
    } | ConvertTo-Json
    Set-Content -Path $SentinelFile -Value $sentinel -Encoding UTF8
} else {
    Write-Host "[Step 6] Onboarding sentinel exists, skipping."
}

$ModelDir = Join-Path $ModelsRoot $InfoJson.models[0].dir_name
if (-not (Test-Path $ModelDir)) {
    New-Item -ItemType Directory -Path $ModelDir -Force | Out-Null
}

# ── Step 7: start OpenVINO inference service ──
Write-Host ""
Write-Host "[Step 7] Starting OpenVINO inference service (port 18765)..."

$ServerPort = 18765
$ServerAlreadyRunning = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ServerPort/api/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction SilentlyContinue
    if ($resp.StatusCode -eq 200) {
        $ServerAlreadyRunning = $true
    }
} catch {}

if (-not $ServerAlreadyRunning) {
    $ServerPy = Join-Path $ScriptDir "server.py"
    Start-Process -FilePath $Python -ArgumentList $ServerPy -WindowStyle Minimized
    Write-Host "Inference service started, loading model in background..."

    # Wait until ready (poll health up to 60s)
    $deadline = (Get-Date).AddSeconds(60)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ServerPort/api/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction SilentlyContinue
            if ($resp.StatusCode -eq 200) {
                $health = $resp.Content | ConvertFrom-Json
                if ($health.status -eq "ok" -or $health.status -eq "downloading") {
                    $ready = $true
                    break
                }
            }
        } catch {}
    }
    if ($ready) {
        Write-Host "Inference service up. Model status: $($health.status)"
    } else {
        Write-Host "Warning: inference service start timed out, pet may not answer yet."
    }
} else {
    Write-Host "Inference service already running, skipping start."
}

# ── Step 8: start pet frontend ──
Write-Host ""
$PetRunning = $false
try {
    $procs = Get-Process -Name "electron", "MiniCPM*", "Clawd*" -ErrorAction SilentlyContinue
    if ($procs) { $PetRunning = $true }
} catch {}

if (-not $PetRunning) {
    Write-Host "[Step 8] Starting pet frontend (npm start)..."
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if ($npmCmd) {
        # Launch via cmd /c so env vars propagate correctly to the child
        # Direct Start-Process on npm can drop the current shell env
        $env:MINICPM_BACKEND = "openvino"
        Start-Process -FilePath "cmd" -ArgumentList "/c", "cd /d `"$PetDir`" && set MINICPM_BACKEND=openvino&& npm start" -WindowStyle Minimized
        Start-Sleep -Seconds 3
        Write-Host "Pet frontend started (backend: OpenVINO)."
    } else {
        Write-Host "Warning: npm not found, cannot start pet frontend."
    }
} else {
    Write-Host "[Step 8] Pet frontend already running, skipping start."
}

# ── Deploy done: verify and print structured result ──
Write-Host ""
Write-Host "=============================================="
Write-Host " Deploy done, verifying..."
Write-Host "=============================================="
Write-Host ""

# Verify inference service
$deployServerStatus = "error"
$deployModelStatus = "error"
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ServerPort/api/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction SilentlyContinue
    if ($resp.StatusCode -eq 200) {
        $health = $resp.Content | ConvertFrom-Json
        $deployServerStatus = $health.status  # ok / downloading / loading / error
        if ($health.status -eq "ok") {
            $deployModelStatus = "loaded"
        } elseif ($health.status -eq "downloading") {
            $deployModelStatus = "downloading"
        } elseif ($health.status -eq "loading") {
            $deployModelStatus = "loading"
        }
    }
} catch {
    $deployServerStatus = "timeout"
}

# Verify pet frontend
$deployPetStatus = "not_running"
try {
    $procs = Get-Process -Name "electron", "MiniCPM*", "Clawd*" -ErrorAction SilentlyContinue
    if ($procs) { $deployPetStatus = "running" }
} catch {}

# Structured verify summary (agents parse this block for success/failure)
Write-Host "[DEPLOY_RESULT]"
Write-Host "server_status=$deployServerStatus"
Write-Host "server_port=$ServerPort"
Write-Host "pet_frontend=$deployPetStatus"
Write-Host "model_status=$deployModelStatus"
Write-Host "[/DEPLOY_RESULT]"
Write-Host ""

# Human-readable summary
if ($deployServerStatus -eq "ok" -and $deployPetStatus -eq "running") {
    Write-Host "Deploy succeeded! You can chat with the pet now."
} elseif ($deployServerStatus -eq "downloading" -or $deployModelStatus -eq "downloading") {
    Write-Host "Inference service up, model downloading in background (~1.5GB)."
    Write-Host "Chat once the download finishes. Check progress with --status."
} else {
    Write-Host "Warning: deploy may not have fully succeeded."
    Write-Host "  inference service: $deployServerStatus"
    Write-Host "  pet frontend: $deployPetStatus"
    Write-Host "  Run scripts\run.ps1 --debug for diagnostics."
}
Write-Host ""
