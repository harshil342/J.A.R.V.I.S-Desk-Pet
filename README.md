<p align="center">
  <img src="assets/readme%20logo.png" alt="MiniCPM Desk Pet / J.A.R.V.I.S." width="760">
</p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg" alt="License"></a>
  <a href="https://huggingface.co/openbmb/MiniCPM5-1B-GGUF"><img src="https://img.shields.io/badge/Model-MiniCPM5--1B-green" alt="MiniCPM5-1B"></a>
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/Protocol-Model%20Context%20Protocol%20(MCP)-orange" alt="MCP">
  <img src="https://img.shields.io/badge/Privacy-100%25%20Offline%20%26%20Local-success" alt="Privacy">
</p>

<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <strong>J.A.R.V.I.S. Desk Pet</strong> is an ultra-fast, local-first AI companion that lives on your desktop. Powered by MiniCPM (1B edge LLM), native screen/window perception, long-term episodic memory, and the open <strong>Model Context Protocol (MCP)</strong>.
</p>

---

## 🚀 Highlights & Capabilities

- **🔒 100% Local & Private** — All model inference, screen OCR, and memory run completely offline on your device with 0 API tokens and 0 telemetry.
- **🔌 Native Model Context Protocol (MCP)** — Seamlessly plug in external MCP servers (Filesystem, GitHub, Brave Search, Postgres, etc.) via JSON-RPC 2.0 stdio transport.
- **🛠️ Schema-Based Tool Engine** — 20+ built-in OpenAI-compatible micro-tools with type-safe execution and error isolation.
- **👁️ Active Screen & Context Perception** — Instant active window inspection (IDE file, process name) and ultra-fast (<15ms) native OCR text extraction.
- **🧠 Semantic Episodic Memory** — Long-term categorical memory with hybrid semantic search (TF-IDF & character/word n-gram cosine similarity).
- **⏰ Proactive Task Dispatcher** — Async background timers, reminders, and proactive pet check-in alerts that wake up your desktop companion.
- **🐾 PetDex & Multi-Pet Personalization** — Choose from a rich library of animated pet characters and skins from the PetDex catalog (cats, robotic orbs, retro sprites, anime companions) and pair them with customized LoRA persona adapters for different users and moods.
- **🤖 Coding-Agent Observability** — Real-time animations and speech bubble summaries reacting to Cursor, Claude Code, and Codex tasks.

---

## 🛡️ Stability & Conversation Reliability (August 2026)

The assistant layer was hardened around a simple rule: **the keyword router owns what it can match; the 1B model only shapes prose.**

- **Leak-proof replies** — a cross-chunk scrubber (`_TagScrubber`) strips any `<function>/<param>/<tool_call>` markup the model leaks, no matter how SSE splits it, and poisoned history can no longer teach the model to parrot failures.
- **Reminders that work** — "set a timer **for two minutes** to stretch" parses (word numbers, any phrasing); a missing detail is asked for **once**, and your follow-up ("two minutes to stretch") completes it. Fired reminders land as **real Windows toast notifications** (PowerShell WinRT fallback keeps them visible even in dev runs).
- **Weather without loops** — "what's my weather" asks for a city once; a bare place name ("Mumbai") answers immediately with live data relayed verbatim.
- **Honest knowledge lookups** — "who is X", "nikola tesla death", "how did he die"-style follow-ups route deterministically: Wikipedia first (progressively trimmed), web search second, relayed verbatim — never a hallucinated refusal.
- **No spurious tool calls** — casual statements ("my codename is Blue Falcon") arm only memory tools, so the model stops inventing system checks; facts still get saved quietly.
- **English-only UI** — every user-visible string (bubble right-click menu, dialogs, Telegram status) is English.

Dev extras: `DESKPET_REMOTE_DEBUGGING_PORT=9222 npm start` + `node tools/verify-bubble-ui.mjs` drives the real bubble over CDP and asserts the weather flow end-to-end in the DOM. A `graphify` knowledge graph (`graphify-out/`) maps this repo for fast architecture queries — see `AGENTS.md`.

### 🎛️ Proactive drawer & memory viewer (August 2026)

The bubble's slim row gained an **⏰ pill**: a drawer with two tabs over the gateway's REST stores —

- **Reminders** (`/api/tasks`): create with a one-line form (what + minutes), see each task's status and ETA, cancel with ×; non-recurring tasks flip to completed after firing through the dispatcher → bridge → Windows-toast pipeline.
- **Memory** (`/api/memory`): everything Jarvis remembers, with semantic search-as-you-type (≥2 chars), inline add, and forget-on-×.

Both round-trips are gated by `npm run smoke` (create → list → fire → delete, plus add → search → forget).

### 🗺️ Roadmap
1. ~~**Durable reminders**~~ ✅ **Done (August 2026)** — timers persist to `Documents/DeskPet/pending-reminders.json`; a gateway restart re-arms future ones and fires overdue ones immediately with an "Overdue reminder" label. "cancel my reminders" clears the store.
2. ~~**Better search fallback**~~ ✅ **Done (August 2026)** — when DuckDuckGo has no instant answer, searches fall back to Wikipedia with relevance-ranked full-text lookup (`list=search` beats opensearch: 'zdzislaw pawlak rough sets' → Pawlak himself, 'nikola tesla die' → Nikola Tesla), and wiki-backed results are relayed verbatim instead of being rephrased by the model.
3. ~~**UI smoke tests in CI**~~ ✅ **Done locally (August 2026)** — `npm run smoke` (`tools/run-ui-smoke.mjs`) spawns the app with CDP enabled, drives `verify-bubble-ui.mjs` against the real bubble (weather flow + no markup leaks), and kills the app; exit code gates it. Wire into a hosted workflow when one has GPU/model access.
4. ~~**Module splits**~~ ✅ **Done (August 2026)** — `minicpm-chat.js` (3.4k → 2.6k lines) shed `minicpm-sidecar-manager.js` (locators, adapter seeding, Sidecar process manager) and `minicpm-history-store.js` (pure v2 session-store helpers, both re-imported so eval-based tests keep binding); `server.py` shed `gateway/tag_scrubber.py`. All names stay importable from their old homes.

### 🚢 Production hardening completed (August 2026)

The Windows installer (`dist/Deskpet-0.11.0-x64.exe`) and macOS bundles are verified end-to-end:

1. ~~**Code signing & trust**~~ ✅ **Done** — Windows Authenticode (`WIN_CSC_LINK`/`CSC_LINK`) & macOS Apple Developer ID notarization wired into CI/CD release workflows.
2. ~~**GitHub Release hosting**~~ ✅ **Done** — Cross-platform GitHub Actions release pipeline (`.github/workflows/release.yml`) builds and publishes per-version artifacts.
3. ~~**Auto-update target**~~ ✅ **Done** — `electron-updater` unified with the production repository release feed.
4. ~~**Installer size**~~ ✅ **Done** — Slimmed `extraResources` packaging filters to ship only `llama-server.exe`, `minicpm-sidecar.exe`, and necessary runtime DLLs (trimmed extraneous CLI/benchmark binaries).
5. ~~**First-run model acquisition**~~ ✅ **Done** — Dual-source model downloader in `minicpm-model-download.js` (Hugging Face + ModelScope mirror failover with chunk verification).
6. ~~**Hardware breadth**~~ ✅ **Done** — Verified multi-backend heuristics with dynamic runtime fallbacks across NVIDIA CUDA, Vulkan GPU, and stable CPU.
7. ~~**Native MCP Client Integration**~~ ✅ **Done** — Open Model Context Protocol stdio client with REST endpoints (`/api/mcp/servers`) and Electron preload IPC bridges.

---

## 🏰 Architectural & Product Moats

Why DeskPet is fundamentally different from generic cloud AI chatbots:

| Moat Dimension | DeskPet / J.A.R.V.I.S. | Cloud AI Chatbots (ChatGPT / Claude) |
| :--- | :--- | :--- |
| **Privacy & Security** | **100% Offline & On-Device.** Code, stack traces, and documents never leave your machine. | Data is transmitted and processed on external cloud servers. |
| **Context Latency** | **Sub-15ms Local Perception.** Native OS APIs read window titles and screen OCR directly. | Requires sending multi-megabyte screenshots across the network. |
| **Cost & Independence** | **$0 / Unlimited Inference.** Runs indefinitely without API keys, credits, or subscriptions. | Pay-per-token or monthly recurring subscription fees. |
| **Desktop Presence** | **Ambient Desktop Companion.** Lives as an interactive animated pet on your screen with speech-bubble reactions and a Mini edge-dock mode. | Hidden inside a browser tab or separate standalone window. |
| **PetDex Personalization** | **Different Pets for Different People.** Swap animated character skins and LoRA personas via PetDex. | Sterile, impersonal generic chatbot UI. |
| **Extensibility** | **Open MCP Ecosystem.** Plug in any community or enterprise Model Context Protocol server. | Walled-garden plugin ecosystems with rigid approval flows. |
| **Token Efficiency** | **OCR-First 1B Architecture.** Feeds clean, structured facts into small models without token saturation. | Consumes 2,000+ visual tokens per screenshot on heavy vision models. |

---

## 🎯 Key Use Cases

### 1. Zero-Copy Terminal & Error Debugging
> *"What does the error on my screen mean?"*
- J.A.R.V.I.S. reads the active IDE window, extracts the visible compiler error or stack trace via native OCR in <15ms, and provides a concise 2-sentence fix without you ever touching `Ctrl+C` / `Ctrl+V`.

### 2. Context-Aware Pair Programming
> *"Help me write a test for this file"*
- DeskPet detects that you are actively working in `server.py` in VS Code and grounds its code generation specifically around your active file context.

### 3. Ambient Agent Supervision
- While background tools (Cursor, Claude Code, Codex) run long multi-file builds, DeskPet animates its thinking/working states and posts a speech-bubble summary the second the job completes.

### 4. Long-Term Knowledge & Semantic Fact Recall
> *"Remember that our staging database is on port 5433"* ➔ *"What port does staging use?"*
- Hybrid semantic memory retrieves relevant technical notes, personal preferences, and project credentials using semantic concept matching.

### 5. Proactive Background Tasks & Timers
> *"Remind me to check the deployment in 10 minutes"*
- Schedules asynchronous background tasks that trigger thought-bubble alerts when due.

### 6. Different Pets for Different People (PetDex)
> *Personalize your desktop experience to match your personality and work style:*
- **The Engineer (J.A.R.V.I.S. / Tech Orb):** A futuristic assistant for monitoring background builds, debugging stack traces, and executing tools.
- **The Cozy Companion (Neko / Cat):** An expressive animated pet with playful banter, encouraging reactions, and gentle posture/break reminders.
- **The Retro Sprite:** Classic pixel-art companions for nostalgic desktop vibes.
- **Custom LoRA Adapters & Skins:** Switch characters, adjust personas, or import community sprite sheets in **Settings -> Themes & Petdex**.

---

## 🧩 Architectural Overview

```
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │                        DESKPET J.A.R.V.I.S. SYSTEM TOPOLOGY                      │
 └──────────────────────────────────────────────────────────────────────────────────┘
                                          │
                 ┌────────────────────────┴────────────────────────┐
                 ▼                                                 ▼
     ┌────────────────────────┐                        ┌────────────────────────┐
     │  Electron Desktop Host │◄─── IPC / HTTP (18765) ───►│ Python FastAPI Gateway │
     │  (clawd-on-desk UI)    │                        │ (minicpm-sidecar)      │
     └────────────────────────┘                        └────────────────────────┘
                 │                                                 │
                 ├── Floating Pet Animation Window                 ├── llama-server (MiniCPM5-1B)
                 ├── Thought Bubble & Chat Renderer                ├── ToolRegistry & Dispatcher
                 ├── Global Hotkeys (Ctrl+Shift+M)                 ├── Native MCP Client Engine
                 └── Mini Edge-Dock Mode                           ├── Screen Context & OCR
                                                                   ├── Semantic Memory Store
                                                                   └── Proactive Task Dispatcher
```

---

## 🛠️ Built-in Tool Catalog & MCP Support

DeskPet includes an extensive suite of native tools, fully exposed via OpenAI-compatible JSON schemas:

### Native Tools
- 🪟 `get_active_window`: Inspects foreground process, window title, and active open file.
- 🔍 `read_screen_text` / `inspect_screen`: Extracts readable text and error context from the screen.
- 🧠 `remember_fact` & `recall_fact`: Stores and semantically retrieves facts from long-term memory.
- ⏰ `schedule_task` / `set_reminder`: Async background timer and reminder dispatcher.
- 🌐 `web_search`, `fetch_page`, `open_url`, `wikipedia_summary`: Local research & browser navigation.
- 📊 `system_status`: Hardware diagnostics (CPU, RAM, Battery, Disk).
- 🧮 `calculate`, `convert_currency`, `convert_units`: Math & financial conversion tools.
- 📝 `create_document`, `todo_*`: Markdown documents and task management.
- 📋 `clipboard_assist`: Inspects system clipboard content.
- 🔒 `lock_workstation`, `take_screenshot`, `media_control`: OS automation utilities.

### Model Context Protocol (MCP)
DeskPet can connect to any external MCP server over stdio. Configure your servers in `Documents/DeskPet/mcp_servers.json`:

```json
[
  {
    "name": "filesystem",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\User\\Projects"],
    "enabled": true
  },
  {
    "name": "github",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
    },
    "enabled": true
  }
]
```

---

## 💻 Getting Started

### System Requirements

| OS | Hardware Requirements | Disk Space |
| :--- | :--- | :--- |
| **Windows** | Windows 10/11 x64 (DirectX12 / Vulkan support) | ~2.5 GB (including 1B GGUF weights) |
| **macOS** | macOS 14.0+ Apple Silicon (M1/M2/M3/M4) | ~2.5 GB (including 1B GGUF weights) |

### Installation

#### Windows
1. Download the latest installer from [Releases](https://github.com/OpenBMB/MiniCPM-Desk-Pet/releases).
2. Launch the app and complete the guided first-run onboarding wizard.
3. The app will automatically configure the sidecar gateway and download `MiniCPM5-1B-GGUF`.

#### macOS
1. Download `MiniCPM Desk Pet-*-arm64.dmg` from [Releases](https://github.com/OpenBMB/MiniCPM-Desk-Pet/releases).
2. Drag **MiniCPM Desk Pet** into `Applications` and launch.

---

## ⌨️ Shortcuts & Controls

- `Ctrl/Cmd + Shift + M` — Toggle MiniCPM Chat Bubble
- `Ctrl/Cmd + Shift + T` — Toggle Thinking Mode
- `Esc` — Close Bubble when focused
- `Right-Click Pet` — Open Context Menu (Settings, Adapters, Model Manager, Quit)

---

## 🔌 Developer REST API

The FastAPI sidecar gateway exposes a comprehensive REST interface on port `18765`:

- `POST /api/chat` — Streaming chat (SSE). Accepts `tool_mode`: `"auto"` (default: keyword router first, then a native function-calling round), `"regex"`, `"native"`, or `"off"`. Tool executions surface as `{"event":"tool","name":...}` SSE frames before the reply deltas. The default can also be pinned via the `MINICPM_TOOL_MODE` environment variable.
- `GET /api/health` — Gateway + llama-server status, including `llama_restarts` and a `degraded` flag from the in-gateway crash watchdog.
- `GET /api/tools` — Get active schema catalog of native and dynamic MCP tools.
- `POST /api/tools/call` — Execute any tool by name with arguments (`{"name": "...", "arguments": {...}}`).
- `GET /api/mcp/servers` — List active MCP servers and connection health.
- `POST /api/mcp/servers` — Add and connect a new external MCP server.
- `GET /api/memory` / `POST /api/memory/search` — Query long-term semantic memory.
- `GET /api/tasks` / `POST /api/tasks` — Schedule proactive timers and background tasks.

---

## 🧪 Running Tests

DeskPet has a comprehensive test suite (170+ Python tests, 4300+ Node tests):

```powershell
# Run all gateway tests
uv run --project minicpm-sidecar pytest minicpm-sidecar/tests

# Run the Electron-side test suite
cd clawd-on-desk && node test/run-tests.js
```

---

## 📄 License

Distributed under the [GNU AGPL-3.0-only](./LICENSE).  
MiniCPM model weights are licensed under the [OpenBMB MiniCPM Model License](https://github.com/OpenBMB/MiniCPM/blob/main/MiniCPM%20Model%20License.md).
