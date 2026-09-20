# Agent Runtime Architecture

This document holds the deeper runtime and integration notes that were previously in the root `AGENTS.md`.

## Data Flow

```text
Claude Code state sync (command hook, non-blocking):
  Claude Code fires event
    → hooks/clawd-hook.js (zero-dep Node script, reads JSON from stdin for session_id + source_pid)
    → HTTP POST 127.0.0.1:23333/state { state, session_id, event, source_pid, cwd }
    → src/server.js route → src/state.js state machine (multi-session tracking + priority + min display time + sleep sequence)
    → IPC state-change event
    → src/renderer.js (<object> SVG preload + fade switch + eye tracking)

Copilot CLI state sync (command hook, non-blocking):
  Copilot fires event
    → hooks/copilot-hook.js (camelCase event names → agents/copilot-cli.js mapping → HTTP POST)
    → same state machine as above

Cursor Agent state sync (command hook, stdin JSON, non-blocking):
  Cursor IDE fires event
    → hooks/cursor-hook.js (hook_event_name → PascalCase event + HTTP POST, stdout returns allow/continue to satisfy preToolUse etc.)
    → same state machine (agent_id: cursor-agent)

Codex CLI state sync (official hooks primary + JSONL fallback):
  Codex fires SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop
    → hooks/codex-hook.js (stdin JSON, session_id preferably aligned with the rollout UUID in transcript_path)
    → HTTP POST 127.0.0.1:23333/state { state, session_id, event, turn_id, hook_source }
    → same state machine (agent_id: codex)
  Codex writes ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    → agents/codex-log-monitor.js (fallback: events hooks don't cover, hooks disabled/unavailable, history compat)
    → main.js wrapper suppresses events for hook-active sessions to avoid duplicate states/bubbles

Gemini CLI state sync (hook-only, stdin JSON + stdout JSON):
  Gemini CLI fires SessionStart / BeforeAgent / BeforeTool / AfterTool / AfterAgent / SessionEnd etc.
    → hooks/gemini-hook.js (hook_event_name or argv event name → agents/gemini-cli.js mapping)
    → HTTP POST 127.0.0.1:23333/state
    → same state machine (agent_id: gemini-cli)

Antigravity CLI (agy) state sync (hook-only, stdin JSON + stdout JSON):
  agy fires PreInvocation / PostToolUse / PostInvocation / Stop
    → hooks/antigravity-hook.js (camelCase payload + argv event name → agents/antigravity-cli.js mapping)
    → HTTP POST 127.0.0.1:23333/state (state)
    → same state machine (agent_id: antigravity-cli)
  Hook registers under the clawd hook group in ~/.gemini/config/hooks.json, **state events only**. PreToolUse is **deliberately not registered**; permissions stay fully with agy's own 5-option native menu (agy 1.0.1 LLM triggers the built-in ask_permission tool, incl. "Persist to settings.json" standing rules). Stop returns allow-stop JSON on stdout.

Kiro CLI state sync (per-agent hook, stdin JSON):
  Kiro CLI fires event
    → hooks/kiro-hook.js (camelCase events → agents/kiro-cli.js mapping → HTTP POST)
    → same state machine (agent_id: kiro-cli)
  Note: Kiro has no global hooks; hooks/kiro-install.js injects hooks into each
  custom agent config under ~/.kiro/agents/, plus maintains a "clawd" agent (inherits kiro_default,
  resynced from kiro_default at startup to avoid behavior drift). Built-in kiro_default has no editable JSON; users need `kiro-cli --agent clawd`
  or `/agent swap clawd` to enable hooks.

CodeBuddy state sync (Claude Code compatible hook, command):
  CodeBuddy fires event
    → hooks/codebuddy-hook.js (PascalCase events → agents/codebuddy.js mapping → HTTP POST)
    → same state machine (agent_id: codebuddy)
  Hook registers in ~/.codebuddy/settings.json, fully Claude Code compatible.

Kimi Code CLI (Kimi-CLI) state sync (hook-only, config.toml):
  Kimi Code CLI (Kimi-CLI) fires event
    → hooks/kimi-hook.js (hook event → agents/kimi-cli.js mapping → HTTP POST)
    → same state machine (agent_id: kimi-cli)
  Hook registers as [[hooks]] entries in ~/.kimi/config.toml; Clawd auto-syncs them at startup.

opencode state sync (in-process plugin, ~0ms latency):
  opencode fires events (session.created / session.status / message.part.updated etc.)
    → hooks/opencode-plugin/index.mjs (Bun runtime, plugin runs inside the opencode.exe process)
    → translateEvent mapping (opencode v2 event names → PascalCase Clawd event names)
    → session.created's event.properties.info.parentID is recorded as a child → parent mapping; child state reports carry headless: true
    → fire-and-forget HTTP POST 127.0.0.1:23333/state
    → same state machine (agent_id: opencode)

Pi state sync (global extension, state-only):
  Pi fires session_start / before_agent_start / tool_call / tool_result / agent_end etc.
    → ~/.pi/agent/extensions/clawd-on-desk/index.ts (Pi extension runtime)
    → hooks/pi-extension-core.js maps to PascalCase Clawd event names
    → HTTP POST 127.0.0.1:23333/state
    → same state machine (agent_id: pi)

OpenClaw state sync (in-process plugin, state-only):
  OpenClaw fires session_start / model_call_started / before_tool_call / after_tool_call / model_call_ended etc.
    → hooks/openclaw-plugin/index.js (plain ESM default object, recognized directly by the OpenClaw plugin loader)
    → mapped to PascalCase Clawd event names, POST body sends allowlisted fields only
    → fire-and-forget HTTP POST 127.0.0.1:23333/state
    → same state machine (agent_id: openclaw)

Hermes Agent state sync (Python plugin, Hermes SDK):
  Hermes fires on_session_start / pre_llm_call / post_llm_call / pre_tool_call / post_tool_call / on_session_end / on_session_finalize / on_session_reset
    → hooks/hermes-plugin/__init__.py (plugin runs inside the Hermes worker process)
    → mapped to Clawd events + sync HTTP POST 127.0.0.1:23333/state
    → same state machine (agent_id: hermes)
  Terminal-focus metadata resolves the process tree async on a daemon thread at plugin register; the first hook may omit source_pid.

opencode permission bubble (event hook + reverse bridge, non-blocking):
  opencode requests permission → event hook receives permission.asked
    → plugin POSTs /permission (with bridge_url + bridge_token) → Clawd 200-ACKs immediately (no held connection)
    → Clawd creates a bubble window → user picks Allow/Always/Deny
    → Clawd POSTs the plugin's reverse bridge → bridge calls opencode's built-in Hono route /permission/:id/reply via ctx.client._client.post()
    → opencode applies the behavior (once/always/reject)

Remote SSH state sync (reverse port forward):
  Claude Code / Codex CLI on the remote server
    → hooks POST to local 127.0.0.1:23333 over the SSH tunnel
    → same state machine (CLAWD_REMOTE=1 mode skips PID collection)

Permission decision flow (Claude Code HTTP hook, blocking):
  Claude Code PermissionRequest
    → HTTP POST 127.0.0.1:23333/permission { tool_name, tool_input, session_id, permission_suggestions }
    → main.js creates a bubble window (bubble.html) showing the permission card
    → user clicks Allow / Deny / suggestion → HTTP response { behavior }
    → Claude Code applies the behavior
    → requests from subagents (Task) carry agent_id (instance uuid) / agent_type; server-agent-id.js normalizes to
      claude-code and marks the subagent origin; with `agents["claude-code"].subagentPermissionsEnabled=false` (#451
      child switch) the connection is dropped so CC falls back to terminal prompting (ExitPlanMode / AskUserQuestion exempt)

Permission decision flow (Codex official PermissionRequest command hook, blocking):
  Codex PermissionRequest
    → hooks/codex-hook.js POSTs /permission { tool_name, tool_input, tool_input_description, session_id, turn_id }
    → default intercept mode: main.js creates a normal Allow / Deny bubble; on click codex-hook.js writes the sanitized allow/deny JSON to stdout
    → explicit native mode: server records a notification and returns no-decision immediately; Codex AutoReview / native approval continues
    → on DND / disabled / bubble hidden / Clawd unavailable, stdout "{}", Codex falls back to native approval
```

## Multi-Agent Registry

Each agent is a config module exporting event mappings, process names, and capability declarations (`capabilities` with `httpHook` / `permissionApproval` / `sessionEnd` / `subagent`):

- `agents/claude-code.js` — Claude Code event mapping + capabilities (hooks, permission, terminal focus)
- `agents/codex.js` — Codex CLI official hook event mapping + JSONL fallback polling config
- `agents/copilot-cli.js` — Copilot CLI camelCase event mapping
- `agents/cursor-agent.js` — Cursor Agent (hooks.json) event mapping
- `agents/gemini-cli.js` — Gemini CLI hook event mapping
- `agents/antigravity-cli.js` — Antigravity CLI (agy) hook event mapping (state-only, no permission bubble)
- `agents/kimi-cli.js` — Kimi Code CLI (Kimi-CLI) hook event mapping + permission classification policy
- `agents/kiro-cli.js` — Kiro CLI event mapping (camelCase), no HTTP hook / no permission / no subagent
- `agents/codebuddy.js` — CodeBuddy event mapping (PascalCase, Claude Code compatible), permission supported
- `agents/opencode.js` — opencode event mapping + capabilities (plugin, permission, terminal focus)
- `agents/pi.js` — Pi extension event mapping + capabilities (extension, state-only, no permission takeover)
- `agents/openclaw.js` — OpenClaw plugin event mapping + capabilities (state-only, local terminal focus not yet supported)
- `agents/hermes.js` — Hermes Agent plugin event mapping + capabilities (session, SessionEnd, terminal focus; no permission/subagent)
- `agents/registry.js` — agent registry: look up agent config by ID or process name
- `agents/codex-log-monitor.js` — Codex JSONL fallback incremental poller (file watch + incremental read + approval heuristic)
- `agents/gemini-log-monitor.js` — legacy Gemini session JSON poller; not started on the current hook-only path

Runtime agent install intent / start-stop / permission-bubble switches go through `src/agent-gate.js`, which reads `prefs.agents[id].integrationInstalled` / `.enabled` / `.permissionsEnabled`.`enabled` only gates whether that agent's events are handled: disabling stops `state.js` / `server.js` from handling events and cleans up sessions / bubbles; only `integrationInstalled` records whether the local hook/plugin/extension is maintained by Clawd.Missing snapshot fields default conservatively to true for old-version compat; fresh-install schemas explicitly mark Claude Code / Codex as installed+enabled and all other agents as not-installed+disabled.Claude Code additionally has a `.subagentPermissionsEnabled` child switch (#451, only the claude-code default entry carries it) controlling whether Task-subagent PermissionRequests pop bubbles.

## Hook And Plugin Sync

The startup chain only backfills missing integrations with `integrationInstalled=true` and `enabled=true`:

- `server.js` async-syncs installed+enabled Claude / Codex / Gemini / Antigravity / Cursor / CodeBuddy / Kiro / Kimi / Qwen / Qoder hooks, opencode / OpenClaw / Hermes plugins, and the Pi extension after startup; Hermes sync first does a side-effect-free install probe and never creates `~/.hermes` when uninstalled
- Claude hook sync also sweeps `DEPRECATED_CORE_HOOKS` (currently `WorktreeCreate`) to remove stale clawd hook entries left by old versions — only the entry whose command points at `clawd-hook.js`; user-written hooks on the same event are untouched

Install on the Settings Agent page runs the matching sync and commits `integrationInstalled=true, enabled=true` together; Uninstall calls the marker-scoped uninstaller and commits `integrationInstalled=false, enabled=false` together. Re-enabling an uninstalled agent alone only opens the event inlet and never writes local config; manual install commands are mainly for debugging, reinstalls, or remote-machine deploys.

## Permission Bubble

- Claude Code / CodeBuddy PermissionRequests use the HTTP hook (blocking); all other events use command hooks (non-blocking)
- Codex PermissionRequest is an official command hook; the hook script suspends waiting on `/permission`, then writes sanitized allow/deny JSON to stdout
- `POST /permission` takes `{ tool_name, tool_input, session_id, permission_suggestions }`; Codex additionally sends `turn_id`, `tool_input_description`, `tool_input_fingerprint`
- Each permission request creates its own `BrowserWindow`; multiple bubbles stack bottom-right upward
- Bubbles report their real height via IPC `bubble-height`; the main process re-layouts from that
- Supports Allow / Deny / suggestion decisions, plus `addRules` / `setMode` suggestion types
- DND only means "don't pop bubbles", never decides permissions for the user: the opencode branch silent-drops so the TUI built-in permission prompt takes over; the Claude Code branch `res.destroy()`s so CC falls back to built-in chat/terminal confirm; the Codex branch returns no-decision `{}` so native Codex approval takes over
- Codex JSONL approval notify bubbles stay only for fallback sessions where official hooks are unavailable; old passive notifies on hook-active sessions are suppressed by the main.js wrapper
- Changes touching Claude Code permission payloads (`permission_suggestions`, `updatedPermissions`, elicitation inputs, etc.) must be verified at least once against real Claude Code; hand-made `curl` requests have historically masked field-shape bugs

### Codex official hook notes

Actual payload edges captured by the P0 spike (2026-04-26, Windows native Codex CLI):

- `session_id` matches the rollout UUID in the `transcript_path` filename; `codex-hook.js` still prefers extracting the UUID from `transcript_path` defensively.
- `permission_mode` is present in all sampled SessionStart / UserPromptSubmit / PreToolUse / PermissionRequest / PostToolUse / Stop events, value `default`.
- `SessionStart.source` sampled as `startup`; other events carry no `source`.
- `Stop.stop_hook_active` sampled as `false`; on `true` the hook no-ops to avoid Codex stop-continuation edge jitter.
- Plain `PreToolUse` / `PostToolUse` `tool_input` is not guaranteed to have `description`; Bash and `apply_patch` samples carry only `command`.
- `PermissionRequest.tool_input.description` exists in real approval samples and is the first choice for bubble copy; falls back to formatted `tool_input` when missing.
- Codex PermissionRequest output must omit `updatedInput` / `updatedPermissions` / `interrupt`, never write `null`; those fields fail closed today.

## Plugin Notes

opencode, OpenClaw, and Hermes integrate as plugins; OpenClaw Phase 1 only reports state, the rest are mostly hook scripts.

- Process-tree walk starts at `process.pid`, not `ppid`
- The `task` tool creates a fresh session directly instead of emitting a subtask part; only sessions whose `session.created` explicitly carries `event.properties.info.parentID` count as children
- opencode child sessions are handled as root-owned background headless work: excluded from HUD / focus / multi-session fanout, `session.idle` degrades to `sleeping/SessionEnd`, only the root session's `session.idle` maps to `attention/Stop`
- Since the `permission.ask` hook is never called on opencode 1.3.13, permissions only go via event hook + reverse bridge
- POSTs from inside the plugin must be fire-and-forget to avoid slowing the TUI
- After packaging, rewrite `app.asar/` to `app.asar.unpacked/`
- The Hermes plugin uses sync POST so short-lived `hermes -z` processes don't drop events before exit; short cooldown when Clawd is down to avoid port-scan churn
- Hermes `agent_pid` is currently the plugin worker PID; `source_pid` comes from async process-tree resolution, used for terminal focus
- Hermes config.yaml is user YAML — no line-oriented edits; install only copies the managed plugin files and runs `hermes plugins enable clawd-on-desk`

## Pi Notes

- Pi uses the global extension dir `~/.pi/agent/extensions/clawd-on-desk`; the installer copies `pi-extension.ts` and self-contained `pi-extension-core.js`
- The extension runs outside the Clawd repo, so it can't depend on `hooks/shared-process.js`; needed process-tree and HTTP logic stays inside the extension files
- Only reports state when `ctx.hasUI === true` or interactive TTY mode, so print/RPC modes don't pollute pet state
- Pi is state-only: `tool_call` only reports `PreToolUse` state, never waits on Clawd `/permission`, pops no permission bubble, and never calls `ctx.ui.confirm()`
- If an old managed extension still inside a running Pi process requests `/permission`, the server answers allow, keeping Pi's default YOLO behavior instead of turning the fallback into manual confirm
- The `tool_call` handler must catch at top level and return `undefined`; Pi's `emitToolCall()` doesn't catch extension errors, and an uncaught one can surface as generic `Extension failed, blocking execution`
- `tool_result` splits by `isError` into `PostToolUse` / `PostToolUseFailure`
- Pi permission subgate defaults off: `prefs` defaults `agents.pi.permissionsEnabled` to `false`; v4 migration resets old true values to false

## OpenClaw Notes

- Phase 1 supports state animation only; OpenClaw `requireApproval` / permission bubbles are out of scope.
- Phase 1 explicitly targets local single-process shapes like `openclaw tui --local`; gateway / daemon / messaging deploys have no stable terminal anchor — designed later.
- Plugin dir is `hooks/openclaw-plugin/`; the manifest must include `activation.onStartup` and an empty-object `configSchema`.
- The installer only ever writes `~/.openclaw/openclaw.json` (or `OPENCLAW_CONFIG_PATH`) when it already exists and `JSON.parse`s cleanly; on JSON5/comment/$include it skips startup sync, and only manual `npm run install:openclaw-plugin` takes the OpenClaw CLI fallback.
- Startup sync never creates `~/.openclaw/openclaw.json`. When OpenClaw isn't installed or initialized it returns skip, avoiding a preemptive broken config.
- On Windows OpenClaw is usually `node.exe ... openclaw.mjs`, so `agents/openclaw.js` declares no process name. OpenClaw's install scanner blocks plugins containing `child_process`; the Phase 1 plugin does no process-tree walk and only sends `agent_pid`, so Sessions Dashboard terminal focus is unavailable for OpenClaw for now.
- After a successful `model_call_ended`, `Stop` goes out on a 1500ms debounce; new model/tool/compaction activity in between cancels it. `failureKind=aborted|terminated` also counts as non-error `Stop`; only timeout/connection failures emit `StopFailure`.
- `session_end` maps to `SessionEnd/sleeping` only on `idle|daily|deleted|unknown`; `new|reset|compaction` never puts the pet to sleep.
- OpenClaw POST bodies are an allowlist: `agent_id`, `session_id`, `state`, `event`, `cwd`, `agent_pid`, `tool_name`, `tool_use_id`, `hook_source`, `openclaw_*`, `error_present`, etc.; never forward `params` / `result` / `error` strings / `messages`.

## Terminal Focus And Remote

- Hook scripts locate the terminal app PID by walking the process tree via `getStablePid()` (Windows Terminal, VS Code, iTerm2, etc.)
- Don't substitute `process.ppid` as a cheap replacement: inside the Claude Code / hook process chain it's usually just a temp shell PID — unstable and unpersistable
- `source_pid` rides along with state updates to `main.js` for Sessions-menu focus
- Right-clicking a Sessions submenu entry focuses the terminal via PowerShell (Windows) or `osascript` (macOS) in `focusTerminalWindow()`
- Remote setups use `scripts/remote-deploy.sh` + SSH reverse port forwarding to relay remote hook events back to local Clawd

## Context Menu Owner Window

- `contextMenuOwner` must keep `parent: win`; parentless plus `closable: false` breaks `app.quit()` teardown
- The quit path depends on `requestAppQuit()` setting `isQuitting = true` first, then letting `window-all-closed` reach the real exit branch; don't bypass this guard

## Updating

- Git mode (unpackaged, mostly macOS/Linux source runs) `git fetch`es and compares HEAD, then `git pull`s + `npm install`s when needed, then `app.relaunch()`
- Windows NSIS packaged mode uses `electron-updater`
- "Check for Updates" in the tray menu triggers manually

## i18n

- Supports en / zh / ko
- Copy lives in `src/i18n.js`
- Language pref persists to `clawd-prefs.json`, hydrated into the controller at startup via `hydrate()`
