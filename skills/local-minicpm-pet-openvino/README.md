# Local-MiniCPM-Pet-OpenVINO

One-click deploy of the MiniCPM desk-pet experience with the OpenVINO inference backend, fully local on Intel AIPC.

## What is this

A Skill for Intel AIPC developers to quickly set up the full MiniCPM desk-pet experience:

- **Frontend**: MiniCPM Desk Pet (Electron pet app, started from source via `npm start`)
- **Backend**: OpenVINO inference service (FastAPI HTTP service, replaces the default llama-server)
- **Model**: MiniCPM5-1B INT8 quantized (auto-downloaded from ModelScope)

All inference runs locally on Intel hardware, no cloud needed. After deploy, chat with the pet directly in its UI.

## Layout

```
local-minicpm-pet-openvino/
├── README.md                 ← this file
└── src/
    ├── SKILL.md              ← Skill metadata + agent runbook
    ├── info.json             ← runtime config (venv name, Python version, memory needs, model URL)
    ├── meta.json             ← store listing (name, description, use-case tags)
    ├── requirements.txt      ← Python deps (openvino-genai, fastapi, uvicorn, etc.)
    └── scripts/
        ├── run.ps1           ← deploy entry: env check → deps → start inference → start pet
        └── server.py         ← OpenVINO inference HTTP service (FastAPI, port 18765)
```

## How it works

```
┌────────────────┐   HTTP :18765   ┌────────────────────┐   OpenVINO   ┌──────────┐
│  桌宠前端       │ ──────────────→ │  server.py (FastAPI)│ ──────────→ │ MiniCPM5 │
│  (Electron)    │ ←────────────── │  (常驻后台)          │ ←────────── │ INT8 模型 │
└────────────────┘                 └────────────────────┘             └──────────┘
```

1. `run.ps1` is the deploy entry: hardware check → env setup → dep install → service start → frontend start
2. `server.py` stays resident as the HTTP inference service with OpenAI-compatible `/v1/chat/completions`
3. The pet frontend talks to the inference service over HTTP; chat in the pet UI

## Usage

This Skill is used via an AI assistant (agent); the user just chats in natural language, no manual commands.

### Step 1: Install the Skill

Drop the whole `local-minicpm-pet-openvino` folder into your AI assistant's Skills directory, or install via the marketplace.

### Step 2: Tell the assistant

Once installed, just tell the AI assistant what you need, e.g.:

- "Deploy the MiniCPM desk pet for me"
- "Set up a local AI desk-pet experience"
- "Run an OpenVINO desk pet on this Intel machine"

The assistant picks up the intent and starts deploying with this Skill.

### Step 3: Wait, then use it

The assistant does all of this with no manual steps:

1. Detect your network env (China auto-uses mirrors)
2. Check Intel AIPC hardware
3. Install Python / npm deps
4. Download the AI model (~1.5GB, first time)
5. Start the OpenVINO inference service
6. Launch the pet frontend window

When done, the pet window appears on the desktop — just talk to it.

### Daily management

After deploy, tell the assistant anytime:

- "Stop the pet" → stops inference + frontend
- "Is the pet still running?" → checks status and reports
- "Restart the pet" → redeploys (idempotent, skips done steps, starts in seconds)

## Requirements

- Windows 10/11 + Intel AIPC hardware (LNL/ARL/PTL/WCL)
- Python 3.11+
- Node.js 18+ / npm
- git

## API endpoints (after deploy)

| Endpoint | Method | Notes |
| --- | --- | --- |
| `/api/health` | GET | health check (model load status) |
| `/v1/chat/completions` | POST | OpenAI-compatible chat inference |
| `/api/shutdown` | POST | graceful shutdown |

---

## Developer / troubleshooting reference

> Below is for developers debugging manually only; end users can ignore.

Manually invoke the deploy script:

```powershell
# Deploy and start (mainland China)
scripts\run.ps1 --china

# Deploy and start (direct internet)
scripts\run.ps1

# Check status
scripts\run.ps1 --status

# Stop all services
scripts\run.ps1 --stop

# Diagnostics (for troubleshooting)
scripts\run.ps1 --debug
```

Script flags:

| Flag | Notes |
| --- | --- |
| `--china` | pin mainland-China mirrors (GitCode / Tsinghua pip / Taobao npm / npmmirror Electron) |
| `--status` | show inference + pet frontend status |
| `--stop` | stop inference + pet frontend |
| `--debug` | verbose diagnostics (see below) |

`--debug` output:

| Group | Info |
| --- | --- |
| System | Windows version, CPU arch |
| Python env | venv present, Python version, openvino/fastapi pkg versions |
| Model | model dir present, file list + sizes |
| Inference | /api/health response, port 18765 occupancy (netstat) |
| Pet frontend | electron process names + PIDs |
| Env vars | MINICPM_BACKEND, PIP_INDEX_URL, ELECTRON_MIRROR, etc. |
| Recent logs | last 20 lines of newest log file |

Health check:

```powershell
curl http://127.0.0.1:18765/api/health
```

Logs: `%USERPROFILE%\.openvino\log\`
