---
name: local-minicpm-pet-openvino
description: |
  One-click deploy of the MiniCPM desk-pet experience with the OpenVINO inference backend (Deploy MiniCPM Desk Pet with OpenVINO backend).
  This Skill deploys a full pet experience locally on Intel AIPC, no cloud needed.
  After deploy, the user chats with the pet directly in its UI.
  Use this skill when the user wants to deploy/set up/run the MiniCPM desk pet with OpenVINO backend,
  or build a local AI pet experience environment on Intel hardware.
  Trigger on: 部署桌宠/体验环境/搭建环境/本地推理/OpenVINO后端/桌宠环境/deploy pet/setup environment/run desk pet/
  英特尔/intel/AIPC/本地/离线/offline/MiniCPM/OpenVINO/桌宠.
  This is a DEPLOYMENT skill — it sets up the environment and launches the pet.
  After deployment, the user interacts with the pet directly through its UI (not via this script).
---

# Local-MiniCPM-Pet-OpenVINO Skill Guide

> This Skill **one-click deploys** a full MiniCPM desk-pet experience.
> Backend: OpenVINO inference engine; frontend started from source via `npm start`.
> After deploy, chat with the pet directly — no need to call this script again.

---

## !! CRITICAL: Environment dependencies !!

All of the following are required — verify before running:

| Dependency | Min version | Check | Purpose |
| --- | --- | --- | --- |
| Windows 10/11 | - | - | OS |
| Intel AIPC hardware | LNL/ARL/PTL/WCL | `bin\platform.exe --is-aipc` | local inference |
| Python | 3.11 | `python --version` | OpenVINO inference service |
| Node.js | 18+ | `node --version` | pet frontend |
| npm | - | `npm --version` | pet dep install |
| git | - | `git --version` | fetch pet source |

---

## !! CRITICAL: Mainland-China network !!

**All network ops must use China mirrors**, otherwise very slow or unreachable:

| Operation | China mirror | Notes |
| --- | --- | --- |
| git clone | GitCode mirror | `gitcode.com/OpenBMB/MiniCPM-Desk-Pet.git` |
| pip install | Tsinghua mirror | `mirrors.tuna.tsinghua.edu.cn` |
| npm install | Taobao mirror | `registry.npmmirror.com` |
| Electron download | npmmirror | `npmmirror.com/mirrors/electron/` |
| Model download | ModelScope | direct domestic link by default |
| HuggingFace | hf-mirror | `hf-mirror.com` |

**The `--china` flag bundles all of the above — no manual setup needed.**

---

## !! CRITICAL: Run outside sandbox !!

These dirs **must persist** (outside sandbox), or every retry re-downloads ~2.4GB:

| Dir | Size | Contents |
| --- | --- | --- |
| `%USERPROFILE%\.openvino\venv\` | ~500MB | Python venv |
| `%USERPROFILE%\.openvino\models\` | ~1.5GB | AI model files |
| `%LOCALAPPDATA%\electron\Cache\` | ~180MB | Electron binary cache |
| pet source `node_modules\` | ~200MB | npm deps |

---

## Strict runbook (in order, no skipping)

### Step 0: Detect user network (agent must do this before calling)

**Before calling `scripts\run.ps1`, the agent must detect the user's network env.**

Pick any signal:
- User explicitly says mainland China / domestic
- User OS locale is Chinese (zh-CN)
- User timezone is UTC+8 and not Singapore/HK/TW
- Direct access to GitHub / PyPI / npmjs.org fails

**Mainland China → add `--china`; otherwise omit.**

### Step 1–8: run automatically via run.ps1

**The agent must invoke the script as follows** (no double-clicking the ps1):
```powershell
# Mainland China (one command does the full deploy)
powershell -ExecutionPolicy Bypass -File "scripts\run.ps1" --china

# Outside China / direct internet
powershell -ExecutionPolicy Bypass -File "scripts\run.ps1"

# Pin inference device (optional: NPU/GPU/CPU, default auto-detect)
powershell -ExecutionPolicy Bypass -File "scripts\run.ps1" --china --device NPU
```

Or use the `run.cmd` wrapper (double-clickable):
```
scripts\run.cmd --china
```

The script completes these steps automatically:
1. Hardware check (Intel AIPC)
2. Mirror config (pin China mirrors with `--china`)
3. Create Python venv + install OpenVINO deps
4. Fetch pet source (git clone)
5. Install pet npm deps
6. Pre-write Onboarding Sentinel (skip the wizard)
7. Start OpenVINO inference service (HTTP, port 18765)
8. Start pet frontend (npm start)

**After deploy, chat with the pet directly in its UI.**

### Step 9: Verify the deploy (agent must do this)

**After the deploy script finishes, the agent must verify success — never just report "done".**

Check with:

```powershell
scripts\run.ps1 --status
```

Pass criteria (parse script output):
- Contains `推理服务: 运行中` **and** `桌宠前端: 运行中` → success, report to user
- Contains `推理服务: 未运行` → failed, follow the troubleshooting guide below
- Contains `桌宠前端: 未运行` → frontend not started, retry with `scripts\run.ps1`

The script also prints a structured summary at the end, e.g.:
```
[DEPLOY_RESULT]
server_status=ok|error|timeout
server_port=18765
pet_frontend=running|not_running
model_status=loaded|downloading|error
[/DEPLOY_RESULT]
```

Agent should parse the summary:
- `server_status=ok` + `pet_frontend=running` → success
- `server_status=error` → run `scripts\run.ps1 --debug` for diagnostics
- `model_status=downloading` → model still downloading, tell the user to wait

If `--debug` can't pin it down, show the debug output to the user for help.

---

## Deployed architecture

```
┌────────────────┐   HTTP :18765   ┌────────────────────┐   OpenVINO   ┌──────────┐
│  桌宠前端       │ ──────────────→ │  server.py (FastAPI)│ ──────────→ │ MiniCPM5 │
│  (Electron)    │ ←────────────── │  (常驻后台)          │ ←────────── │ INT8 模型 │
└────────────────┘                 └────────────────────┘             └──────────┘
```

- `server.py` serves `/v1/chat/completions` (OpenAI-compatible) and `/api/health`
- The pet frontend talks to the inference service over HTTP
- The model auto-downloads from ModelScope on first run (~1.5GB)

---

## Flags

| Flag | Notes |
| --- | --- |
| `--china` | pin mainland-China mirrors, no network probing |
| `--device NPU\|GPU\|CPU` | pin inference device (default auto-detect, prefers GPU) |
| `--status` | show current status (inference + pet frontend) |
| `--stop` | stop everything (inference + frontend, incl. leftover processes) |
| `--debug` | verbose diagnostics (Python env, model files, ports, logs) |

**Notes:**
- The agent must call via `powershell -ExecutionPolicy Bypass -File` (Windows blocks bare .ps1 by default)
- git clone auto-sets `GIT_LFS_SKIP_SMUDGE=1` to skip LFS blobs (models download separately via server.py)
- CPU check: non-Intel CPUs are blocked; non-Core-Ultra warns but continues

Lifecycle example:
```powershell
# 部署并启动
scripts\run.ps1 --china

# 查看状态
scripts\run.ps1 --status

# 不想跑了，停止所有服务
scripts\run.ps1 --stop

# 再次启动（幂等，跳过已完成的步骤）
scripts\run.ps1 --china
```

---

## Exit Codes

| Exit Code | Meaning |
| --- | --- |
| 0 | deployed, pet running |
| 1 | generic error (unsupported HW, missing env, network failure) |

---

## Troubleshooting (agent reference on error)

### Inference down (server_status=error or timeout)

1. Run `scripts\run.ps1 --debug` for diagnostics
2. Check the Python-env section: is openvino-genai installed?
3. Check port-18765 occupancy: kill the holder and retry if taken
4. Check recent logs: look for a Python traceback or ImportError
5. Common causes:
   - Incomplete Python deps → delete venv and redeploy
   - Port taken → `netstat -ano | findstr :18765`, kill the PID
   - OpenVINO vs HW mismatch → confirm Intel AIPC (LNL/ARL/PTL/WCL)

### Frontend down (pet_frontend=not_running)

1. Confirm Node.js 18+ and npm: `node --version`
2. Confirm the `MINICPM_BACKEND=openvino` env var is set
3. Check npm install finished (is there a node_modules dir?)
4. Re-run `scripts\run.ps1` (idempotent, retries the frontend)
5. If the frontend sticks on the onboarding wizard: `MINICPM_BACKEND` wasn't passed through — check env vars

### Model download timeout (model_status=downloading)

1. Not an error — the model is ~1.5GB, first download takes a while
2. Confirm `--china` was used (domestic ModelScope link is faster)
3. Poll with `scripts\run.ps1 --status` to see if the service is still downloading
4. The service auto-loads the model when done, no extra step needed

### Generic recovery

```powershell
# 1. 查看完整诊断信息
scripts\run.ps1 --debug

# 2. 停止所有服务
scripts\run.ps1 --stop

# 3. 重新部署（幂等，已完成的步骤会跳过）
scripts\run.ps1 --china

# 4. 验证
scripts\run.ps1 --status
```

Logs: `%USERPROFILE%\.openvino\log\`

---

## What this Skill does NOT do

- No dialogue prompt params (chat happens in the pet UI)
- No cloud calls
- No non-Intel platforms
- No big downloads inside a sandbox
- No prebuilt .exe installer (runs from source)
