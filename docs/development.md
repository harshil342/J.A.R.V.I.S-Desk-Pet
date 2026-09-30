# Developer Guide

> Regular users: just download the dmg installer (see [README.md](../README.md)) and follow the first-launch Onboarding wizard. This document is only for developers who need to modify code / debug / build packages.

---

## Table of Contents

- [Quick Start (dev mode)](#quick-start-dev-mode)
- [Repository Structure](#repository-structure)
- [Packaging: From Source to dmg](#packaging-from-source-to-dmg)
- [Onboarding Flow: Developer Notes](#onboarding-flow-developer-notes)
- [Common Debugging Tips](#common-debugging-tips)

---

## Quick Start (dev mode)

```bash
git clone https://github.com/harshil342/J.A.R.V.I.S-Desk-Pet.git
cd MiniCPM-Desk-Pet

# Drop the model in here (~2 GB; unlike production users, dev mode still uses <repo>/models/)
mkdir -p models
# Models are downloaded by the onboarding wizard, not symlinked. There is no 0.9b
# model; the current presets are MiniCPM5-1B and MiniCPM5-2B.
# or cp -r

./go.sh                  # auto-install dependencies + launch
```

`./go.sh doctor` checks the environment only; `./go.sh setup` installs dependencies without launching.

### Skipping Onboarding (Developer Privilege)

The Onboarding wizard pops up on first launch. If you already placed the model, just click through (the environment check passes automatically; the model-download step detects the local path and shows "already exists").

To bypass the wizard on a given launch (e.g. when debugging the settings tab):

```bash
# Onboarding sentinel lives under userData
Remove-Item "$env:APPDATA\deskpet\minicpm-onboarding.json"   # Windows
rm "$HOME/Library/Application Support/Deskpet/minicpm-onboarding.json"   # macOS

# or the reverse: force it to show again
MINICPM_FORCE_ONBOARDING=1 ./go.sh start
```

---

## Repository Structure

```
MiniCPM-Desk-Pet/
├── clawd-on-desk/              ← Electron desktop pet (vendored fork of clawd-on-desk@5b1f003)
│                                  + MiniCPM integration layer: chat bubbles / Onboarding / Settings
├── minicpm-sidecar/            ← llama.cpp inference service + thin FastAPI gateway
├── adapters/                   ← LoRA adapters (.gguf + safetensors source)
├── skills/deploy-minicpm-pet/  ← Cursor Agent Skill (dev deployment guide)
├── docs/                       ← developer docs
├── models/                     ← GGUF model files (gitignored)
├── go.sh                       ← developer shortcut script
└── README.md                   ← user-facing (dmg install + guide)
```

> The duplicate PyTorch sidecars from the v0.7 era (`minicpm-pet-bridge/` and `minicpm-pet-bridge-uv/`) plus the PyInstaller `build/sidecar.spec` were removed in v0.9. For the current sidecar layout and build method, see [`minicpm-sidecar/README.md`](../minicpm-sidecar/README.md).

---

## Packaging: From Source to dmg

The dmg shipped to end users contains:
- Electron main app (clawd-on-desk)
- PyInstaller-built sidecar binary (no user Python install required)
- LoRA adapters (adapters/)
- sidecar source (fallback / debug use)

Model weights are **not** in the dmg — first launch guides the user via Onboarding to download them into `<userData>/models/`.

### One-Step Build

```bash
./go.sh build
```

Equivalent to:

```bash
# 1. Download official llama.cpp llama-server + PyInstaller-build the gateway
cd minicpm-sidecar && ./scripts/build-all.sh && cd ..

# 2. electron-builder outputs the dmg
cd clawd-on-desk
npx electron-builder --mac --arm64 -c.mac.target=dmg
```

Build output: `clawd-on-desk/dist/Deskpet-<version>-x64.exe`.

### Repack dmg Only (Without Re-running PyInstaller)

After editing Electron-side code / package.json, the sidecar binary does not need rebuilding:

```bash
cd clawd-on-desk && npm run build:mac:repack
```

## Windows Packaging (x64, Supported)

One command produces the NSIS installer (auto-builds the PyInstaller gateway first):

```powershell
cd clawd-on-desk
npm run build:win:mvp
```

Equivalent to:

```powershell
# 1. PyInstaller-build the gateway → minicpm-sidecar/bin/win-x64/minicpm-sidecar.exe
#    (uv sync aligns dependencies per pyproject.toml, including winotify)
powershell -NoProfile -ExecutionPolicy Bypass -File ..\minicpm-sidecar\scripts\build-gateway.ps1

# 2. electron-builder outputs the NSIS installer
cd clawd-on-desk && npx electron-builder --win nsis:x64
```

Build output: `clawd-on-desk/dist/Deskpet-<version>-x64.exe`.

Packaging layout contract (at runtime the gateway locates llama-server relative to its own directory; see the frozen branch in `gateway/llama_client.py`):

- `<install-dir>/resources/sidecar-bin/minicpm-sidecar.exe`
- `<install-dir>/resources/sidecar-bin/llama-server.exe` (or `backends/<cpu|vulkan|cuda>/llama-server.exe`)

This is handled automatically by the `extraResources` entry `../minicpm-sidecar/bin/win-x64 → sidecar-bin` in package.json.
The llama.cpp binaries are downloaded into the same staging directory via `minicpm-sidecar/scripts/fetch-llama-release.ps1`.

### Windows Packaging Verification Checklist

Minimal E2E last verified 2026-08, against v0.11.0. Since then the version is
0.12.0 and the target list changed to Windows x64 only, so re-run this before
trusting it. The full checklist is in `plan.md` Phase 1.

1. Launch after install: gateway process path must be `...\Programs\Deskpet\resources\sidecar-bin\minicpm-sidecar.exe`
2. Chat routing: `who is X` → wikipedia tool hit
3. Settings sync: `POST /api/config` to the gateway takes effect (returns echo)
4. Reminder flow: `remind me in N seconds to X` → bridge push + winotify native toast on time (no "native toast failed" in logs)
5. userData: clawd-prefs.json / minicpm-chat-history.json / onboarding sentinel file generated correctly under `%APPDATA%/deskpet/`

Note: gateway dependencies must be declared in `minicpm-sidecar/pyproject.toml` — `build-gateway.ps1` runs `uv sync` first,
so manually pip-installed but undeclared packages get wiped and PyInstaller misses them (winotify hit this pitfall before).

### Current Packaging Limitations (MVP)

Rewritten 2026-09-30. Several items below used to say the opposite of the truth.

- **Windows x64 (NSIS) is the only supported target** (decision D3). macOS and
  Linux are no longer built at all. This is deliberate: `win.extraResources`
  could not vary per architecture, so the arm64 installer that was being
  published contained **x64** native binaries, and the flagship sidecar tools —
  OCR, screenshot, media keys, volume, `launch_app`, `wifi_info` — are
  Windows-only by construction. Shipping a shell around a non-functional core is
  worse than shipping nothing.
- **The build is signed, and signing is a functional requirement, not a
  cosmetic one.** electron-updater verifies the Authenticode signature of the
  downloaded `.exe`, so an unsigned release installs once and then silently
  stops receiving updates. `npm run sign:dev` creates a self-signed certificate,
  `npm run sign:check` proves it can sign, and `npm run build:win:signed`
  builds with it. A self-signed certificate cannot buy SmartScreen reputation,
  so users still click *More info → Run anyway*; a commercial certificate is
  what removes that.
- Auto-update **is** wired up: `src/updater.js`, backed by `electron-updater`,
  with a 12-hour background scheduler. The release workflow publishes a DRAFT
  when no certificate is configured, because a public unsigned release would
  strand every existing user.
- The **rolling channel** `windows-latest` always names the newest build, so
  install docs can use one URL that never goes stale.
- Model download supports **Hugging Face and ModelScope**, chosen by a
  country lookup, with `MINICPM_MODEL_PROVIDER` able to pin a fixed host.
- macOS notarization and signing code still exists in `scripts/notarize.js` but
  is not exercised, because there is no macOS build. Treat it as unverified.

### Packaging Notes for Networks in China

GitHub Release assets (Electron binaries, dmg-builder bundle) often time out from China. Two speedup options:

**1. npm mirror** — use the Taobao registry when installing dependencies:

```bash
cd clawd-on-desk
npm install --no-audit --no-fund \
  --registry=https://registry.npmmirror.com \
  --electron_mirror=https://registry.npmmirror.com/-/binary/electron/
```

**2. Proxy** — electron-builder has no official mirror config for the `dmg-builder@1.2.0/dmgbuild-bundle-arm64-*.tar.gz` GitHub Release asset, so the whole build must run behind a proxy:

```bash
# .zshrc has a proxy helper (http://127.0.0.1:10808)
proxy
cd clawd-on-desk && npx electron-builder --mac --arm64 -c.mac.target=dmg
```

Or inline:

```bash
cd clawd-on-desk && \
  https_proxy=http://127.0.0.1:10808 http_proxy=http://127.0.0.1:10808 \
  npx electron-builder --mac --arm64 -c.mac.target=dmg
```

If the proxy is unstable, pre-download the dmg-builder bundle into the cache manually:

```bash
CACHE="$HOME/Library/Caches/electron-builder/dmg-builder@1.2.0"
mkdir -p "$CACHE"
https_proxy=http://127.0.0.1:10808 curl -L --retry 5 -o "$CACHE/dmgbuild-bundle-arm64-75c8a6c.tar.gz" \
  "https://github.com/electron-userland/electron-builder-binaries/releases/download/dmg-builder@1.2.0/dmgbuild-bundle-arm64-75c8a6c.tar.gz"
```

Then re-run the build. electron-builder skips the download once the target archive is in the cache.

---

## Onboarding Flow: Developer Notes

Onboarding is a 5-step state machine; main-process + renderer code is laid out as follows:

| File | Responsibility |
|------|------|
| [`clawd-on-desk/src/minicpm-onboarding.js`](../clawd-on-desk/src/minicpm-onboarding.js) | Main process: BrowserWindow management + IPC handlers + sentinel file I/O |
| [`clawd-on-desk/src/minicpm-onboarding.html`](../clawd-on-desk/src/minicpm-onboarding.html) | Static structure for the 5 panels |
| [`clawd-on-desk/src/minicpm-onboarding.css`](../clawd-on-desk/src/minicpm-onboarding.css) | Dark / light theme styles |
| [`clawd-on-desk/src/minicpm-onboarding-renderer.js`](../clawd-on-desk/src/minicpm-onboarding-renderer.js) | Renderer: step switching, progress bar, SSE consumption |
| [`clawd-on-desk/src/preload-minicpm-onboarding.js`](../clawd-on-desk/src/preload-minicpm-onboarding.js) | contextBridge → `window.onboarding` |

Main-process entry point: middle of [`src/main.js` `app.whenReady()`](../clawd-on-desk/src/main.js) — `_minicpmOnboarding.shouldShow()` decides whether to show the wizard or the desktop pet directly.

### Completion Flag (sentinel)

Writing `{complete: true, version: 1, completedAt: <iso>, device: <picked>}` to `<userData>/minicpm-onboarding.json` marks it complete. Deleting that file forces it to reappear (also triggerable via Settings → 🐾 MiniCPM → Advanced / Develop).

### Debugging Individual Steps

```js
// Call directly in the onboarding window's devtools, no need to run the full flow
await window.onboarding.listDevices()
await window.onboarding.checkDisk()
await window.onboarding.warmup()
```

---

## Common Debugging Tips

### Viewing Sidecar Live Logs

```bash
# dev mode: Electron stdout already forwards lines with the [sidecar] prefix
./go.sh start

# packaged mode: sidecar stderr goes into the Electron main-process log
# Windows (the only platform we build)
Get-Content "$env:APPDATA\deskpet\session-debug.log" -Wait -Tail 20
Get-Content "$env:APPDATA\deskpet\logs\sidecar.log" -Wait -Tail 20
```

### Curling the Sidecar Directly

```bash
curl -s http://127.0.0.1:18765/api/health | python3 -m json.tool
curl -s http://127.0.0.1:18765/api/devices | python3 -m json.tool
curl -s http://127.0.0.1:18765/api/onboarding | python3 -m json.tool
curl -X POST http://127.0.0.1:18765/api/set-device -H 'content-type: application/json' -d '{"device":"cpu"}'
```

### Port Conflicts

```bash
lsof -ti:18765 | xargs -r kill -9   # sidecar
lsof -ti:23333 | xargs -r kill -9  # clawd HTTP server
```

Windows (PowerShell):

```powershell
# Check who owns the gateway / llama-server ports
Get-NetTCPConnection -LocalPort 18765,18766 -State Listen |
  Select-Object LocalPort, OwningProcess,
    @{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}}

# One-shot cleanup (the gateway binary is a PyInstaller bootloader; plain Stop-Process leaves children behind,
# so taskkill /T must take the whole process tree)
taskkill /F /IM minicpm-sidecar.exe /T
taskkill /F /IM llama-server.exe /T
```

In dev mode, if 18765 is already taken when `python -m gateway` starts, it prints the taskkill hint above and exits with code **78** instead of throwing an EADDRINUSE stack trace.

### Tool Call Mode (tool_mode)

The `tool_mode` field of `POST /api/chat` controls how local tools are triggered:

| Value | Behavior |
|----|------|
| `auto` (default) | Keyword regex routing first; on miss, one round of native model function calling (up to 3 tools executed, then re-answer with results) |
| `regex` | Regex routing + tool context injection only |
| `native` | Native function calling only |
| `off` | Plain chat, no tools touched |

The default can be overridden with the `MINICPM_TOOL_MODE` env var. If the model chat template does not support tools (e.g. some persona LoRAs), the native round auto-falls back to plain chat. The gateway has a built-in llama-server crash watchdog (exponential backoff, up to 3 restarts), exposed via `llama_restarts` / `degraded` in `GET /api/health`; Electron additionally auto-restarts at the process level (2s→5s→10s), pushing state over `minicpm:sidecar-state` IPC so chat bubbles show "restarting/recovered/offline".

### Adding/Modifying Micro-Tools (Dual-Engine Tool Calling)

DeskPet uses a dual-engine tool-calling architecture. Follow these rules for any new tool:

1. **Deterministic execution + regex routing (`gateway/tools.py`)**:
   - Register regex match patterns and arg extractors in `_TOOL_PATTERNS`.
   - Write the tool executor (add try/except protection, keep `/ponytail` minimal style, prefer stdlib or existing system commands).
   - Add a no-extra-inference natural-language reply for deterministic cases in `canned_reply(tool_name, result)`.
2. **Native function registration with OpenAI JSON Schema (`gateway/tool_registry.py`)**:
   - Register a standard OpenAI function-call spec (`name`, `description`, `parameters`, `properties`, `required`) in `TOOL_SCHEMAS`.
   - Add the matching dispatch branch in `execute_tool(name, arguments)`.
   - This ensures that when users phrase requests with natural language, inversion, or colloquialisms that bypass regex, the local model via `llama-server` can still call the tool via structured `tool_calls` without hallucination.
3. **Perception boost + context parsing (`gateway/screen_context.py`)**:
   - For screen awareness or active-window extraction, extract clean web tab titles (`web_page` field) for mainstream browsers like Chrome/Edge/Firefox, for `canned_reply` or LLM reference.
4. **Test coverage**:
   - `minicpm-sidecar/tests/test_tool_routing.py`: regex matching + reply generation.
   - `minicpm-sidecar/tests/test_tool_registry.py`: schema completeness + dispatch.
   - `minicpm-sidecar/tests/test_screen_context.py`: screen/window perception parsing.
5. **Re-freeze binary + package**:
   - Run `minicpm-sidecar/scripts/build-gateway.ps1` to repackage the sidecar binary.
   - Run `npm run build:win:x64` in `clawd-on-desk` to build the NSIS installer.

### Fully Resetting User Data (Caution: Deletes Models and Chat History)

```bash
Remove-Item -Recurse -Force "$env:APPDATA\deskpet"   # Windows
```
