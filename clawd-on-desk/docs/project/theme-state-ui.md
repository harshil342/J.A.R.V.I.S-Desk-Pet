# Theme, State, And UI Notes

This document holds the state machine, theme system, UI runtime, and platform caveats that were previously embedded in the root `AGENTS.md`.

## Dual-Window Model

The pet uses two independent top-level windows:

- Render window (`win`): transparent full-size window, permanent `setIgnoreMouseEvents(true)`, only renders SVG animation + eye tracking
- Input window (`hitWin`): small rect window, `transparent: true` + `setShape` over the hitbox, `focusable: true`, permanent `setIgnoreMouseEvents(false)`, receives all pointer events

Input flow: `hitWin renderer → IPC → main → renderWin renderer`

This split fixes the Windows drag-dead bug: `WS_EX_NOACTIVATE` + layered window + Chromium child HWND falls into an activation dead-end after z-order changes. With the split, the input window stays `focusable: true` and sidesteps it.

## State Machine

- Multi-session tracking: `sessions` Map records state per `session_id`; `resolveDisplayState()` picks highest priority
- State priority: `error(8) > notification(7) > sweeping(6) > attention(5) > carrying/juggling(4) > working(3) > thinking(2) > idle(1) > sleeping(0)`
- Min display time: prevents flicker (`error=5s`, `attention/notification=4s`, `carrying=3s`, `sweeping=2s`, `working/thinking=1s`)
- One-shot states: `attention/error/sweeping/notification/carrying` auto-fall back after showing (`AUTO_RETURN_MS`)
- Sleep sequence: 20s mouse idle → idle-look → 60s → yawning(3s) → dozing → 10min → collapsing(0.8s) → sleeping; mouse move triggers waking(1.5s) → resume
- DND mode: skips dozing, yawning → collapsing → sleeping; also suppresses hook events
- Hide pet (petHidden, via tray / context menu / hotkey): means "pet invisible", not do-not-disturb — hiding parks the pet, Session HUD, update bubble, and pending permission bubbles (they return when shown again), but new permission requests arriving while hidden still pop normally — by design, don't "fix" it; silencing permission bubbles too is DND's job (it has a return-to-terminal fallback). petHidden doesn't persist; restarts show the pet again
- working sub-animations: Clawd theme is 1 session → typing, 2 → headphones groove, 3+ → building; Calico / Cloudling stay typing / juggling / building
- juggling sub-animations: 1 subagent → juggling, 2+ → conducting

## Theme System

Clawd is a themed pet: animation assets, timing, hitbox, and eye-tracking params all come from theme config.

- Built-in theme dirs: `themes/clawd/`, `themes/calico/`, `themes/cloudling/`; `themes/template/` is the scaffold
- User theme dir: `<userData>/themes/<id>/theme.json`
- `theme.json` required states: `idle`, `working`, `thinking`
- With `eyeTracking.enabled`, the idle asset must be SVG containing `#eyes-js`
- With `sleepSequence.mode: full` (default), `yawning / dozing / collapsing / waking` are required; `direct` enters `sleeping` straight away
- With `miniMode.supported: true`, 8 base mini states are required; `mini-working` is an optional extra, gracefully skipped when missing
- Missing capabilities fall through the `VISUAL_FALLBACK_STATES` chain
- Defaults live in the `DEFAULT_*` constants at the top of `theme-loader.js`
- Variants are allowlist deep-merges; arrays and specific fields replace wholesale
- Animation overrides are per-slot user overrides, orthogonal to author-defined variants
- SVGs pass through allowlist sanitization blocking scripts, event attrs, external resources, `javascript:`, and path traversal
- `trustedRuntime.scriptedSvgFiles` only applies to loader-recognized built-in themes; external themes declaring it are ignored
- Supported formats: SVG / GIF / APNG / WebP / PNG / JPG; cycle length probed by `src/animation-cycle.js`
- Update visuals follow theme bindings: `checking` optionally uses `theme.updateVisuals.checking`, falling back to the current theme's `thinking` when undeclared; new versions enter `available -> notification`; `downloading / success / error` keep using `carrying / attention / error`

Theme creation flow: `docs/guides/guide-theme-creation.md`.

## Settings Panel

Settings is a standalone `BrowserWindow` with 4 layers:

| Layer | File | Role |
|---|---|---|
| Schema / persist | `src/prefs.js` | `SCHEMA` defs; `load/save/migrate/validate`; corrupt files auto-`.bak` + fallback |
| Memory store | `src/settings-store.js` | `createStore()` returns `{ getSnapshot, subscribe, _commit }`; `_commit` closure-private |
| Controller | `src/settings-controller.js` | sole writer; `applyUpdate` / `applyBulk` / `applyCommand` / `hydrate`; pre-commit effect gate |
| UI | `src/settings-renderer.js` + `settings.html` + `preload-settings.js` | theme cards, animation overrides, agent switches, diagnostics; talks to controller via IPC only |

Key tradeoffs:

- `applyUpdate` and `applyBulk` are isomorphic for sync/async effects
- `hydrate()` is the only effect-skipping entry
- Write path is only `controller → store → subscribers`
- About tab uses inline SVG instead of `<object>` because `settings.html` CSP is `default-src 'none'`

## Mini Mode

The character hides at the screen's right edge, window half pushed off-screen, cropped naturally by the edge.

Entry:

- Drag to the right edge (`SNAP_TOLERANCE=30px`) → quick slide-in + `mini-enter`
- Context menu "Mini Mode" → crab-walk to the edge → parabolic jump-in → peek-in entry

Core mechanics:

- `miniMode` intercepts normal states, mapping notification / attention to mini counterparts
- `miniTransitioning` suppresses hook events and peek during entry
- `checkMiniModeSnap()` checks all monitors' right edges
- `miniIdleNow` is independent of `idleNow`: eye tracking only, no sleep sequence
- `animateWindowX()` + `animateWindowParabola()` handle slide and parabola animation
- `savePrefs()` persists `miniMode/preMiniX/preMiniY`

Mini state mapping:

| State | SVG | Purpose |
|------|-----|------|
| `mini-idle` | `clawd-mini-idle.svg` | idle: breathing, blinking, arm sway, eye tracking |
| `mini-enter` | `clawd-mini-enter.svg` | one-shot slide-in bounce |
| `mini-peek` | `clawd-mini-peek.svg` | hover peek |
| `mini-alert` | `clawd-mini-alert.svg` | notification |
| `mini-happy` | `clawd-mini-happy.svg` | done |
| `mini-crabwalk` | `clawd-mini-crabwalk.svg` | crab-walk for menu entry |
| `mini-enter-sleep` | `clawd-mini-enter-sleep.svg` | entry under DND |
| `mini-sleep` | `clawd-mini-sleep.svg` | DND sleep |
| `mini-working` | theme-optional | 1-session mini typing; silently skipped when missing |

## State To Animation Mapping

Authoritative table: `docs/guides/state-mapping.md`. Implementation-only extras here:

- working sub-animations: Clawd theme is 1 session → typing, 2 → headphones groove, 3+ → building; Calico / Cloudling stay typing / juggling / building
- juggling sub-animations: 1 subagent → juggling, 2+ → conducting
- mini states have their own animation slots; `mini-working` is an optional capability
- sleep sequence and DND behavior: see State Machine above
- `attention / error / sweeping / notification / carrying` are one-shot states, falling back via `autoReturn` after showing

## Assets

- Assets are organized per theme: each theme dir bundles its own `assets/`
- `assets/svg/` and `assets/gif/` are the shared root paths used by the default Clawd theme
- Doc preview GIFs live in `assets/gif/`, never read at runtime
- Copy source assets to `assets/source/` before editing
- SVGs render at runtime via `<object type="image/svg+xml">`, other bitmaps via `<img>`
- Default in-SVG IDs: `#eyes-js`, `#body-js`, `#shadow-js`, `#eyes-doze`

## Runtime UI Systems

### Sound

- `app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required")` must be set before window creation
- `playSound(name)` in `main.js` checks `soundMuted`, `doNotDisturb`, and cooldown
- `renderer.js` caches `Audio` objects in `_audioCache`
- `attention/mini-happy` plays complete, `notification/mini-alert` plays confirm

### Eye Tracking

- `tick.js` polls the mouse every 50ms
- Eye displacement quantizes to a 0.5px pixel grid
- Unmoved mouse is dedup-skipped, never sent
- Returning from `idle-look` to `idle-follow` needs `forceEyeResend`
- Current design **deliberately avoids** a cross-process "renderer ready" handshake; the main process keeps emitting `eye-move`, recovery relies on delayed `forceEyeResend` plus renderer-side self-check remount
- Any `!moved` / dedup optimization must keep the `forceEyeResend` bypass, or the eye reposition after idle-look gets swallowed

### Animated SVG Through `<img>`

- The `?_t=` cache-bust query `renderer.js` appends to `<img>` SVGs is required
- Cause isn't HTTP caching: Chromium reuses the document + CSS animation timeline for same-URL SVGs; a `forwards` one-shot animation loads stuck on its last frame the second time
- Related dedup logic must compare normalized filenames, not the final query-bearing URL

### Click Reactions

- Double-click → left/right poke reaction
- 4-click → two-hand pat reaction
- Drag → sustained drag reaction
- Reaction animations temporarily detach eye tracking

## Electron And Platform Notes

- `win.setFocusable(false)`: render window never steals focus
- `hitWin.focusable: true`: input window may activate — the key to the drag-bug fix
- `win.showInactive()`: shows without interrupting user input
- Both render / input windows depend on `backgroundThrottling: false`; unfocused throttling amplifies eye-tracking and input-recovery timing issues
- Paths always via `path.join(__dirname, ...)`
- Transparent borderless floaters: `frame: false`, `transparent: true`, `alwaysOnTop: true`
- Single-instance lock: `app.requestSingleInstanceLock()`
- Position persists to `clawd-prefs.json`
- Multi-monitor clamping via `clampToScreen()` + `getNearestWorkArea()`

## Known Limits

- `hitWin` clicks briefly steal focus — accepted cost for now
- No macOS test machine in the current dev env; all macOS-specific paths are code-review + best-effort inference only, real behavior changes need human verification
- Startup resume depends on `detectRunningClaudeProcesses()` plus later hook events
- Windows foreground lock bypasses via ALT trick + `koffi` FFI, edge failures still possible
- Hook scripts depend on Node.js
- Windows terminal focus depends on `koffi`; macOS on `osascript`
- Codex CLI is official-hooks primary, JSONL polling fallback; WebSearch / compaction / abort and other hook-uncovered events may still lag on polling
- Copilot CLI auto-syncs `<COPILOT_HOME or ~/.copilot>/hooks/hooks.json`; with `disableAllHooks: true` it's a doctor warning with no Fix button
- Gemini has no permission bubble unless a compatible blocking approval protocol appears later; Cursor permissions go over stdout; Kiro has no global hooks; opencode permissions only go via event hook + bridge
- opencode child / subtask sessions count as headless only when `session.created` explicitly carries `event.properties.info.parentID`; such background children stay out of HUD / focus / multi-session fanout
- Process-liveness checks depend on process-name matching; non-standard names may be missed

## Do Not Fix This Again

The Language submenu clipping at the bottom is an Electron transparent-window + Windows DWM low-level compat issue — don't retry fixing it via pure-JS `alwaysOnTop` or transparent-window tweaks.
