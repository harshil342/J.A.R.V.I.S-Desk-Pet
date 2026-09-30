# DeskPet — Production Hardening & Gap Closure Plan

**Repo:** `H:\apps\Deskpet` · **Upstream:** `harshil342/J.A.R.V.I.S-Desk-Pet` (fork of `OpenBMB/MiniCPM-Desk-Pet`)
**HEAD at plan time:** `592fff6` (2026-09-20) · **Last tag:** `v0.11.0` (`7cb06f3`, 2026-08-28) — **12 commits behind HEAD**
**Architecture:** Electron 41 desktop pet (`clawd-on-desk/`) + Python FastAPI/llama.cpp gateway (`minicpm-sidecar/`)
**Target:** 0.12.0 shippable → 0.13–0.15 feature closure

---

## 0. Decisions already locked

These were settled with the owner on 2026-10-01. Everything below is built on them.

| # | Decision | Consequence |
|---|---|---|
| D1 | **Keep the Python sidecar. Harden it, don't migrate.** | Add a clean-venv install test + a frozen-exe import assertion as permanent CI gates. Taby ships single-process and Perry is all-TS, but migrating 165 KB of `tools.py` rewrites working software for zero user-visible gain. |
| D2 | **Delete all voice + sound work. Build a new audio system.** | Theme schema version bump + migration. New `AudioEngine` with a swappable voiceprint asset. See §Phase 2.5. |
| D3 | **Windows x64 only.** | Drop arm64 / macOS / Linux from `package.json`, CI, README, `development.md`. Removes the per-arch `extraResources` bug, the 404 macOS DMG link, the wrong-arch VC++ redist, and the 675 MB installer in one pass. |
| D4 | **Phases 0–2 now. Re-decide 3–5 after 0.12.0 ships.** | Phases 3–5 are a documented roadmap, not commitments. |
| D5 | **Audio: sounds + a few spoken lines, WITH mouth movement synced to the audio.** | Not a notification-only pet, not a chatterbox. The mouth sync is the biggest believability jump per line of code in the whole plan (~30 lines, no assets). |
| D6 | **It speaks when coding agents need you or finish** — not unprompted through the day. | You get spoken lines for the moments that matter and silence otherwise. This is what keeps it from getting muted by day three. |
| D7 | **Snapshot the old voice work to a branch before deleting it.** | Unrecoverable deletions get a safety net. Cost: one commit. |
| D8 | **`gh auth login` provided** so the release hook can be verified end-to-end. | O5 resolved. Signing cert still outstanding. |

### Still open (see §9)

- O1 — Does 0.12.0 ship **with** a minimal sound set, or silent?
- O2 — Does the Phase 2 rules engine get a written threat model first? (recommended: yes)
- O3 — Was `v0.11.0` actually released publicly, and to whom?
- O4 — `clawstick` / Hardware Buddy: revive or delete?
- O5 — GitHub auth + Windows signing certs are not configured on this machine.

---

## 1. Reference projects — what we take, and the license position

Four projects researched in depth. We take **code and patterns only**.

| Project | License | What we take | What we do NOT take |
|---|---|---|---|
| **Perry** (`TheM1N9/perry`) | MIT | Approval-rule model, the Reviewer turn, layered memory, Windows wake scheduling, channel target routing, engine abstraction | — |
| **Coucou** (`Louis-CFM/coucou`) | MIT code / **assets © Louis Raillé, all rights reserved** | Hook fail-open budget + ACK handshake, click-through gate, hook-install diff/fingerprint, poller manifest, sound engine, perf contract, rolling release channel | **The Mochi character, all 28 sounds, artwork, and every asset.** MIT does not extend to them. |
| **Taby** (`heytaby.com`) | Desktop app **closed**; firmware Apache-2.0 | MCP capability model, permission-driven tool discovery, `ask_human` primitive, idempotency + optimistic concurrency, trash, rate limits, agent-facing AI context file, "Minecraft rule" | App code. Only the published contract is readable. |
| **VoiceOS** (`voiceos.com`) | **Closed** | Integration-platform contract, `UI is data`, one-way `requireConfirmation`, secrets-on-demand, audio-capture patterns, the anti-computer-use lesson | Code. Contract and pattern only. |

### The single most important negative result

**VoiceOS has no generic screenshot→click→type loop.** Cloud actions go through Composio/MCP; Apple actions through EventKit / Messages / Mail / Spotlight; text insertion through Accessibility APIs; synthetic input for keys. Screenshots are for *understanding*, not clicking. Their changelog is a month-by-month record of overlay-automation failures — dropped mouse-forwarding hooks, clicks not registering, blank-window recovery after sleep.

→ **We do not build generic computer use.** See §7 "Explicitly not building."

---

## 2. Skill set

### Already installed and in use (no install needed)

| Skill | Governs | Where it applies |
|---|---|---|
| `ponytail` | **Every line we write.** Active every response. | YAGNI ladder, deletion over addition, one runnable check per non-trivial change |
| `ui-ux-pro-max` | UI/UX decisions | 79 styles, 192 palettes, 74 font pairings, 119 UX guidelines, 105 icons, 17 GSAP presets, 22 stacks. Phase 2.5 audio UI, Phase 4 habits/focus surfaces, settings tabs |
| `ui-styling` | Component + token work | shadcn/Radix + Tailwind patterns, accessible components, dark mode, canvas visual designs |
| `design-system` | Token architecture | The `voice.json` / `theme.json` schema shape for the audio system |
| `design` | Brand + iconography | App icon, settings icons, release assets |
| `brand` | Voice + consistency | README/CHANGELOG tone, product naming |
| `graphify` | Architecture queries | `graphify-out/graph.json` (9,890 nodes). **Run `graphify update .` before Phase 3** |
| `slides` | Release communication | Public roadmap + release announcement decks |

### Deliberately NOT installed

| Candidate | Why not |
|---|---|
| `changelog-manager`, `release-skills`, `release-manager` | All three **intercept git commands** and impose their own versioning conventions. The repo already has clean Conventional Commits (`feat:`, `fix:`, `chore:`, `ci:`). Adding an agent that second-guesses `git` to solve a problem a 150-line script solves is exactly the dependency `ponytail` rung 5 forbids. |

**Build instead:** `scripts/release.mjs` + `.github/workflows/release.yml`. See §Phase 1.

---

## 3. Live breakage — fixed in Phase 0, before any feature work

These are from the user's own runtime logs, not speculation. Each is a *current* defect.

| # | Defect | Evidence | Fix |
|---|---|---|---|
| B1 | **Semantic memory fails to load on most startups** | 19 log occurrences, **16 on 2026-09-21**. `semantic_memory.py:134` uses `encoding="utf-8"` with no BOM tolerance. Any BOM → entire store silently discarded at WARNING level. | `encoding="utf-8-sig"`. One line. |
| B2 | **Native tool calling silently disables itself** | 13 occurrences, **12 on 2026-09-21**. `--ctx-size 4096` vs a measured 4925-token prompt (system prompt + 52 tool schemas + history). Each hit degrades to plain chat with no user-visible sign. | Raise ctx to 8192 **and** gate tool schemas by `tool_mode` so `off`/`regex` sends zero schemas. |
| B3 | **`reportlab` undeclared** | Not in `pyproject.toml` **or** `uv.lock`. `pdf_engine.py` imports it at module scope → `ModuleNotFoundError` on any clean install. Works locally only by accident. | Resolve in Phase 2 deletion (§2.5 removes `pdf_engine.py` entirely). |
| B4 | **`kokoro-onnx` / `soundfile` undeclared; `piper-tts` unused** | `piper-tts` is imported nowhere yet drags onnxruntime + three numpy pins, contradicting `pyproject.toml:8-10` "pure stdlib + HTTP shim". | Delete `piper-tts`. Others resolve via §2.5 deletion. |
| B5 | **Hardware probe → onboarding schema mismatch** | Producer returns `primaryGpu{name,vramBytes}` / `totalRamGB` / `reason`. Consumer reads `gpu.model` / `gpu.vramFormatted` / `ramFormatted` / `rationale`. 3 of 4 pills render `CPU (Shared)` on every machine. No test catches it — producer and consumer are asserted against different names. | One shared field-name constant, imported by both. Test asserts both against it. |
| B6 | **25 mojibake lines in `server.py`** | Double-encoded em-dash `â€"`, 4 of them user-facing (`server.py:904,906,1391,1543`). | Re-encode. |
| B7 | **`winotify` missing from the frozen build** | Crash dump #4: `No module named 'winotify'` — reminder toasts silently fail in the packaged app. `docs/development.md:146` predicted this exact class of failure and it recurred. | Build-time assertion that every declared dep imports inside the frozen exe. |
| B8 | **Hook-driven auto-launch is broken on packaged Windows/Linux** | `hooks/auto-start.js:74` spawns `"MiniCPM Desk Pet.exe"`, `:95` spawns `"clawd-on-desk"`. `productName` is `Deskpet` → the binary is `Deskpet.exe`. | Read the name from `product-metadata.js`. One source. |
| B9 | **Version skew blocks all in-app updates** | `package.json` = `0.11.0`; `v0.11.0` tag is 12 commits back. `compareVersions` returns 0 → `update-not-available` → **no user can ever receive an update.** | Bump to `0.12.0` before any build. |

---

## 4. Phase plan

Each phase has a **runnable gate**. A phase without a green gate is not done.

### Phase 0 — Establish truth (~1 day)

Nothing else is trustworthy until this passes.

| Action | Detail |
|---|---|
| Commit the WIP | Branch `wip/pre-0.12.0`, one commit, known-broken items in the message. Return `main` clean. 25 modified + 19 untracked is not a baseline. |
| Delete `piper-tts` | From `pyproject.toml`; regenerate `uv.lock`. |
| Fix B1, B2, B5, B6, B8 | As tabulated above. |
| Bump to `0.12.0` | `package.json` **and** `minicpm-sidecar/pyproject.toml` (currently `0.1.0`, never bumped in any release). |
| Clean-venv test | New test: build a venv from `uv.lock` alone, import every module-scope import in `gateway/`, assert all resolve. |

**Gate:**
- `uv run pytest` green on a venv created from `uv.lock` alone
- `node test/run-tests.js` green
- A new test asserting `uv.lock` covers every module-scope import in `gateway/`
- `git status` clean on `main`

### Phase 1 — Shippable 0.12.0 (~3 days)

| Change | Why |
|---|---|
| **Gate `release.yml` on tests** | Commit `592fff6` added a gate to `build-installer.yml` (push-to-main, publishes nothing). The **tag pipeline that actually publishes has never run a test.** |
| Fix release title | `name: "…v0.11.0 (Production Release)"` is hardcoded while `tag_name` is dynamic → a `v0.12.0` push publishes a release titled v0.11.0. |
| Add `concurrency:` | Absent → double-tagging races two publishing workflows. |
| Sign-or-refuse | If `WIN_CSC_LINK` is absent the artifact publishes **unsigned**, and electron-updater then rejects it at install. Nothing prevents this. |
| **D3: drop arm64 + mac + linux** | `win.extraResources` is a single `bin/win-x64` entry; electron-builder has no per-arch extraResources → the ARM64 installer ships an x64 `llama-server.exe`. macOS x64 is declared but CI builds `--arm64` only, and the README links to a DMG that cannot exist. Also fixes the wrong-arch VC++ redist (only `vc_redist.arm64.exe` is ever downloaded, and installed for x64 too). |
| Installer 675 MB → target < 250 MB | Bundles CPU **and** Vulkan llama-server plus all CUDA DLLs; `themes/` (26.7 MB) + `assets/` (18.1 MB) ship twice (in `files` **and** `asarUnpack`). Drop Vulkan; pick one location per tree. |
| **Rolling release channel** | Adopt Coucou's always-newest URL: `releases/download/windows-latest/Deskpet-Setup.exe`. Removes version-guessing from the installer docs. |
| `scripts/release.mjs` + fixed CI | See §Phase 1.1. |
| **B7: frozen-exe import assertion** | In `build-gateway.ps1` / `.sh` — fail the build if a declared dep doesn't import in the frozen binary. |
| **Repo hygiene** | Untrack `graphify-out/` (24.9 MB generated). Ignore `downloads/`, `brag-output*/`, `.pytest_cache/`, `launch-worker.bat`, `*.pfx`, `scripts/audio_samples/`. |
| CI minimum | `npm ci` (not `install --no-audit`), CodeQL, Dependabot, `node --check` sweep over `src/` (free lint, no config). |
| **README / docs truth pass** | Delete: "100% Offline & Air-Gapped" (model downloader geolocates via Cloudflare/ipapi on first run), "sub-15ms OCR" (PowerShell spawn alone is 150–400 ms), the `deskpet` CLI section (no `bin` field, no executable), the macOS x64 DMG link, the Linux section that `development.md` calls unsupported. Fix: clone URL points at the wrong repo, nonexistent 0.9b model, `Clawd on Desk` paths, "macOS is unsigned" (it *is* notarized), self-contradiction 100 lines apart. |
| Move or delete inert workflows | `clawd-on-desk/.github/workflows/{build,wayland-smoke}.yml` have **never executed** (nested `.github` in a subdir is not read by Actions). Move the Wayland guard to root, or delete. Don't pretend it runs. |

#### Phase 1.1 — The release hook

**What the user asked for: a hook that updates the repo and the GitHub releases.**

`scripts/release.mjs` — one file, no dependencies (Node 20 is already a build prerequisite).

```
node scripts/release.mjs check      # preflight: clean tree, version in sync, tag absent, tests green
node scripts/release.mjs bump <patch|minor|major>
node scripts/release.mjs changelog  # regenerate CHANGELOG.md from Conventional Commits since last tag
node scripts/release.mjs tag        # annotated tag from CHANGELOG section
node scripts/release.mjs push       # push commit, then tag, then gh release create --verify-tag
node scripts/release.mjs roll       # move the windows-latest rolling tag to the new release
```

Safety properties it must have, each with a test:

1. **Refuses on a dirty tree.** Never `git add -A`. Stages only the version + changelog files, named explicitly.
2. **Version is single-sourced.** Bump writes `clawd-on-desk/package.json` + `minicpm-sidecar/pyproject.toml` + `CHANGELOG.md` together, or writes none of them.
3. **Tag is annotated from the changelog section**, not a hand-written string.
4. **Push commit before tag**, then `gh release create --verify-tag`. Documents that ordering so a re-tag is a fast-forward, never a rewrite.
5. **Release body is a file**, passed via `--notes-file`. Never inlined into a shell command.
6. **Idempotent.** Re-running after a partial failure resumes rather than double-tagging.
7. **`--dry-run` on every subcommand.** Prints the exact commands without executing.
8. **Signature gate.** If `WIN_CSC_LINK` is unset, the release is created as a **draft** with a warning — never a public unsigned release.

CI side: `release.yml` gains `needs: test`, `concurrency: {group: release-${{ github.ref }}, cancel-in-progress: false}`, `fail_on_unmatched_files: true`, top-level `permissions: contents: read`, a `concurrency`-scoped `contents: write` on the release job only, and a step that re-verifies artifact SHA-256 against the blockmap before upload.

**Gate:** build a real installer → install on a clean Windows VM → `deskpet doctor` clean → **exercise the updater path once** (it has logged `skip — not packaged` 121 times since July; the whole path is runtime-untested) → `node scripts/release.mjs check` green.

### Phase 2 — Replace the genuinely bad core (~1 week)

The phase that earns the word "production."

| Replace | With | Source |
|---|---|---|
| **Binary `autoApproveAllPermissions`** | Scoped rules `{kind, command, prefix, cwd, pathPrefix, uses, lastUsedAt}`. Match through `CHAINED = /[;&\|`<>\r\n]\|\$\(/` and shell-wrapper stripping (`powershell -Command`, `cmd /c`, `sh -c` — essential on Windows). Derive a prefix only from the engine's own `proposedExecpolicyAmendment` *and* verify it truly is a prefix. Save a rule only on an affirmative, non-timeout answer. Surface `uses` so dead rules are visible. | Perry `convex/approvals.ts` |
| **Hooks that deny when DeskPet is down** | 3-layer budget (300 ms connect / 2 s fire-and-forget / 110 s permission) **+ 800 ms ACK handshake**, so a broken or paused UI costs 800 ms not 110 s. Tri-state `ack \| decision \| decline`. "Paused ⇒ decline so the terminal takes over." **One card at a time** — a second request is handed straight back. **Silence, never a synthesized deny.** | Coucou `pipe.rs`, `hook/src/main.rs` |
| **Hook install writes directly** | preview diff → FNV-1a fingerprint of the shown bytes → **refuse if the file changed** → backup → temp+rename → shell-free quoted path. Read errors are **never** treated as empty (that bug once meant an unreadable `settings.json` got overwritten with only our hooks). Strip UTF-8 BOM. | Coucou `hooks.rs` |
| **Flat, BOM-fragile memory** | 3 tiers: `profile` (instructions) / `core` / `daily` (`YYYY-MM-DD`). Recall = RRF over keyword + semantic, 30-day half-life. **Recalled memory is data, not instructions.** Supersession, not deletion. Citation trailer so the user sees what was remembered. A 30-line `GUIDE` in the system prompt **plus** a per-message nudge — because "told once, an agent answers the question and lets the fact in it go." | Perry `convex/memories.ts` |
| **`route_tools` fabricates results** | `tools.py:3248-3250` — the inner `run()` helper discards `safe_tool_call`'s ok flag, so a **failed** tool becomes a "result" that `canned_reply` reformats into a confident answer. **The worst trust bug in the assistant.** Propagate the error. | — |
| **`remember_fact` / `recall_fact` swallow everything** | `tools.py:2000-2004, 2011-2026` — bare `except: pass`. Facts land in `notes.md` but not semantic memory, so `/api/memory` lies. Log; tell the caller. | — |
| **`_sync_notes_file` rewrites `notes.md` wholesale inside a bare except** | `semantic_memory.py:153-161`. Mid-write failure leaves `notes.md` and `memory_store.json` permanently diverged with no signal. Atomic temp+rename, propagate failure. | — |
| **Unauthenticated sidecar + `CORS allow_origins=["*"]`** | `server.py:539`. Combined with `POST /api/mcp/servers`, **any webpage can spawn arbitrary commands.** Bind-check, token minted at spawn, no wildcard origin. | — |
| **Context degrades silently** | Measured `contextFill`, checkpoint at 75 %, raise ctx, and **surface the degraded state** instead of quietly dropping to plain chat 12×/day. | Perry |
| **4 inert workflows** | `clawd-on-desk/.github/workflows/*` never execute. Move or delete. | — |

**Dead-code deletions:**

`render-canvas.js` (orphaned, zero refs) · `agents/gemini-log-monitor.js` (orphaned, "Legacy only") · `agents/kimi-log-monitor.js` (explicit no-op stub) · `notImplemented()` in `settings-actions.js:542-549` (never called) · the unreachable "Coming soon" panel + 5 locales of `placeholderTitle/Desc` + `.placeholder*` CSS + the reference to a nonexistent `docs/plans/plan-settings-panel.md` · `rowFullscreenOverlay` + desc × 5 locales (control deliberately removed per issue #562) · 9 `console.log` lines in `main.js` · `server.py:1391,1543,1625` hardcoded `"sir"` · `tests/test_output_mapping.py:11,20,29` asserting literal `"sir"` (re-couples tests to what was just made configurable).

**Gate — each with a runnable test:**
- A hook fires with the app stopped → the tool **falls through** instead of being denied
- `git status; rm -rf ~` is **not** covered by a `git status` rule
- A failing tool produces an error reply, not a confident one
- A rule saved on timeout is not persisted
- A preview fingerprint mismatch refuses the write
- `remember_fact` failure is visible, not silent

### Phase 2.5 — Audio replacement (~1 week)

**D2.** This is a theme-schema change with a migration, not a file delete.

#### What the coupling actually is

`DEFAULT_SOUNDS` at `clawd-on-desk/src/theme-schema.js:5-8` is part of the **theme contract**. It reaches into `soundOverrides` (`settings-actions-theme-overrides.js`), the per-user sound picker (`settings-ipc.js:227-328` + `theme-loader.js:232-248`), the 84 KB `settings-tab-anim-overrides.js` editor, and 7 shipped themes. It is the only item in this plan that can break a user's saved theme.

#### Deletions

`gateway/jarvis_soundboard.py` · `tools.speak` + its registry entry + both `/api/soundboard/*` routes + all 4 `play_output_soundboard()` call sites · `scripts/build_jarvis_soundboard.py` · `scripts/build_kokoro_dense_soundboard.py` · `scripts/setup_kokoro_voice.py` · `scripts/interactive_jarvis_voice.py` · `scripts/test_jarvis_speech.py` · `scripts/audio_samples/` · `downloads/paul_bettany_raw.wav` (154 MB) · `assets/sounds/{complete,confirm}.mp3` · `DEFAULT_SOUNDS` · `gateway/pdf_engine.py` + `tools.research_and_generate_pdf` + its registry entry (the hardcoded template that never calls the LLM and fabricates a sentence on fetch failure — Phase 3 re-implements it properly for ~40 lines).

*Note: two competing builders currently write the same `catalog.json`, so the soundboard is whichever ran last.*

#### The replacement

One interface, three layers, zero coupling to the UI:

```
AudioEngine (main process, singleton)
  ├─ clips       preloaded + decoded → one-shot UI/state feedback, zero latency
  ├─ voice       streaming neural TTS for spoken replies
  └─ voiceprint  the timbre identity — a swappable asset, never hardcoded
```

**1. The mapping is data, not code.** Lifted from Coucou's `STATE_SOUND` (`windows/src/mochi/engine.ts`) — a complete MIT table, and *theme* data, so a theme carries its own voice. We replace the vocabulary, not the mechanism.

```json
// themes/<id>/voice.json
{
  "id": "jarvis",
  "voiceprint": {
    "ref": "voiceprints/<name>.wav",
    "provenance": { "source": "", "license": "", "verified": "" }
  },
  "defaults": { "volume": 0.12, "maxVolume": 0.2 },
  "clips":   { "working": "work", "approval": "approval", "finished": "finish" },
  "variants": { "working": ["work_a", "work_b", "work_c"] }
}
```

`provenance` empty → the build refuses that voiceprint. One field, checked by one test, and "where did this audio come from" stops being an archaeology problem. This also means **swapping the reference clip is a 30-second change, not an architecture change** — which is what makes the voice decision reversible.

**2. Never block. Duck, don't mute.** Every play is fire-and-forget off a bounded queue. An approval chime *lowers* background audio — cutting reads as a crash. (VoiceOS's media-ducking lesson.)

**3. Priority, so a real event cuts through.** `approval/error` preempts idle chatter; cap concurrent voices at 2. Without this, a pet with a soundboard is a slot machine.

**4. Mouth sync from RMS — highest payoff per line in this system (D5).** The renderer already drives a spring-damped mouth channel (Coucou's `UploadFrame` carries a `mouth` value for exactly this). `AnalyserNode → RMS → smoothed → mouth height`. ~30 lines, no assets. This is the difference between "a pet that plays sounds" and "a character that talks."

**4a. What it says, and when (D5 + D6).** Two tiers, deliberately different:

| Tier | Trigger | Kind | Budget |
|---|---|---|---|
| **Cue** | any state change: working, thinking, done, error, permission needed, wake, sleep | sound only | 1 clip, ≤ 400 ms |
| **Line** | an agent needs you · an agent finished · you click or talk to the pet | sound **+** one spoken line | ≤ 2.5 s, cancellable |

A cue never speaks. A line is capped and always cancellable by the next event — a pet with an unbounded queue is a pet that talks over you. "Unprompted through the day" is explicitly **out of scope**: no morning chit-chat, no idle commentary. Silence is the default state, and that is the feature.


**5. Stream TTS clause-by-clause.** Latency is time-to-first-clause, not time-to-finish-sentence.

**6. Never play the same clip twice running.** `variants` sampled without immediate repeat. Three extra files per event removes the cheap-alarm quality entirely.

**7. A personality *is* (prompt preset + voiceprint).** Folds into Phase 4. One coherent concept instead of two features.

**8. Voiceprint model selection.** Zero-shot cloning is required (a preset voice cannot be "sounds exactly like" a specific person). Permissively-licensed candidates to evaluate: CozyVoice 2, Chatterbox, IndexTTS-2, Spark-TTS, Kokoro (preset-voice baseline). **Avoid XTTS-v2 (CPML) and Fish-Speech S1 (CC-BY-NC-SA) — both non-commercial, both easy to pick by accident.** Verify each model's *weights* license at implementation time, per Coucou's own golden rule in `INTEGRATIONS.md`.

**Gate:** theme-schema migration tested against all 7 shipped themes + a user theme with saved sound overrides · `voice.json` validation rejects a voiceprint with empty `provenance` · mouth sync drives the renderer channel with no assets · the app boots silent without a voiceprint and says so once in the log.

### Phase 3 — Pluggable Engine *(post-0.12.0, re-decide)*

**Why it's the unlock:** it makes the ChatGPT or Claude subscription a free brain, and it turns two currently-fake features real. `pdf_engine.py` never called the LLM because there is no LLM abstraction — only llama.cpp. A pluggable engine is the root-cause fix, not a patch.

- `runner/engine.ts`-shaped interface: `{ id, capabilities: { steer, compaction, approvals, concurrentTurns, modelSwitchInSession, usage, quickTurns, skills }, start, turn, steer, interrupt, review, kill }`
- Three implementations: **`codex`** (`codex app-server`, NDJSON JSON-RPC over stdio — *not* `codex exec` — with `thread/resume`, `turn/steer`, `account/read`, `model/list`, pre-warmed spare threads), **`claude`** (headless `-p`; we already own the hook side), **`local`** (existing `llama_client.py`).
- Chat routes through the engine; the 42 tools become engine tools, not a regex router bolted in front.
- **The Reviewer:** a separate, tool-less, ephemeral quick turn that sees *only the action, never the conversation* — explicitly so a prompt injection in a web page or file cannot argue its own case. Fail-closed: anything but a clear verdict goes to the human. This is the answer to safe auto-approve.
- **Instructions diffing** (`runner/instructions.ts`, ~100 lines): diff by paragraph-under-heading, inject via `thread/inject_items`. A resumed thread ignores new `developerInstructions`; this is what makes "I edited the system prompt" actually work.
- Turn results written atomically (`.tmp` + rename) **before** delivery; recovered on boot.
- **Then** re-implement `research_and_generate_pdf` as the local model actually writing the brief.

**Gate:** the same question answered through all three engines · instructions diff proven by a test that mutates the prompt and asserts the injected update · Reviewer proven to ignore a prompt injection embedded in the conversation.

### Phase 4 — Product gaps vs Taby / VoiceOS *(post-0.12.0, re-decide)*

| Add | Effort | Why not YAGNI |
|---|---|---|
| **Local STT + push-to-talk** | medium | Biggest real gap. Taby uses NVIDIA Parakeet locally; we already run llama.cpp in a Python sidecar, so whisper.cpp/parakeet is a known quantity. Take VoiceOS's two hard-won fixes free: the **lossless stop handshake** (five releases on "dictation eats your first words") and **known-ASR-hallucination filtering**. Filler removal + Raw/Light/Polished is where perceived quality lives — WER is the wrong metric, and they say so in their own glossary. |
| **`deskpet` CLI** | small | The README already advertises it and it does not exist. `status / open / logs -f / update / pet / doctor / stop`. Our `doctor` is already ~90 % built. This is a broken promise, not a new feature. |
| **Personalities** | trivial | 4–6 prompt + voiceprint presets (§2.5 item 7). Zero infra. Highest delight-per-line on this list. |
| **Habits + daily goals + "perfect day" animation** | small | We have an animated character. Taby gates this behind €8.99. |
| **"When am I most productive?"** | medium | **Nobody has a good version.** We already record per-agent session history with tool-level detail — a richer signal than Taby's day-view fill. A query over data we already have. |
| **Focus / Pomodoro + active hours** | trivial | `powerMonitor` already exists. Per-task `estimatedPomodoros` / `timeSpent` is three fields. |
| **32-day trash** | small | Taby's soft delete. Kills the "I deleted the wrong thing" class of bug permanently. |

UI for all of the above: `ui-ux-pro-max` + `ui-styling` + `design-system`.

### Phase 5 — Agent-facing platform *(post-0.12.0, re-decide)*

What turns "17 integrations" into a platform.

- **MCP capability model (Taby).** Named connections × 8 boolean permissions. **Permission-driven tool discovery** — a tool the connection can't use is *absent from `tools/list`*, not merely denied at runtime. Default connection gets 7 of 16.
- **`ask_human` primitive.** Async prompt; `pending | selected | timed_out | cancelled | skipped`; `409` when busy; **two surfaces of one logical prompt** (bubble + Telegram); first valid selection wins; effect vocabulary `{none, dismiss, open_url, open_settings}`. We have both surfaces and no unified primitive.
- **Concurrency + idempotency.** `Idempotency-Key` with 5-minute retention; `expectedUpdatedAt` → `409 stale`; reconcile-by-poll on stable UUID + `updatedAt`.
- **Anti-hallucination by design.** `GET /api/states` so an agent never invents a pet state. An AI context file with an explicit "do not invent these" list. Published rate limits. Documented degradation order.
- **The Minecraft rule.** `MANIFEST_SCHEMA_VERSION = 1`; additive changes never bump; declare reserved surfaces now so nothing we wrote breaks later.
- **Secrets on demand** (VoiceOS): ask for the key the first time a tool needs it. No settings screen to build.
- **Credential storage.** `telegram-token-store.js` is plaintext-adjacent. → Windows Credential Manager / macOS Keychain with a `KNOWN_KEYS` allowlist; expose only `present()` to the renderer, never the value. (Coucou `secrets.rs`.)
- **Windows wake scheduling** (Perry `server/wake.ts`). A Task Scheduler task running `cmd /c exit` — waking *is* its job — re-registered whenever the target minute changes, plus `powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE` to detect the "wake timers disabled" gotcha and say so. Turns "reminders work" into "reminders work even if the PC was asleep." ~120 lines, reusing our existing wake-recovery.

**Gate:** a third-party script registers, is correctly *absent* from `tools/list`, and is denied a capability it doesn't hold.

### Phase 6 — Cuttable *(only on request)*

External-service pollers refactored to Coucou's manifest (`{id, firstRunDelay, interval, credentialGate, parse, isNew, event}` + global `PAUSED` + silent-first-poll dedupe + errors-never-destroy-good-data) · e-ink/AMOLED companion using Taby's Apache-2.0 firmware · two-way mobile bridge.

**All three are YAGNI for 0.12–0.15.**

---

## 5. Delete / replace ledger

One table. Nothing in the codebase should contradict it.

| Item | Action | Phase |
|---|---|---|
| `clawd-on-desk/src/render-canvas.js` | delete (0 refs) | 2 |
| `clawd-on-desk/agents/gemini-log-monitor.js` | delete (0 refs) | 2 |
| `clawd-on-desk/agents/kimi-log-monitor.js` | delete (no-op stub) | 2 |
| `settings-actions.js:542-549` `notImplemented()` | delete (never called) | 2 |
| `settings-renderer.js` "Coming soon" panel + 5 locales + CSS | delete (unreachable) | 2 |
| `rowFullscreenOverlay` × 5 locales | delete (control removed, issue #562) | 2 |
| `main.js` 9 × `console.log` | delete | 2 |
| `server.py` 25 mojibake lines | fix | 0 |
| `server.py` 3 × hardcoded `"sir"` | replace with `assistant_address` | 2 |
| `piper-tts` dependency | delete (unused) | 0 |
| `gateway/jarvis_soundboard.py` + 5 scripts + `audio_samples/` | delete | 2.5 |
| `downloads/paul_bettany_raw.wav` (154 MB) | delete | 2.5 |
| `gateway/pdf_engine.py` + `research_and_generate_pdf` | delete (never called the LLM) | 2.5 |
| `assets/sounds/*.mp3` + `DEFAULT_SOUNDS` | delete → theme schema v2 + migration | 2.5 |
| `graphify-out/` from git | untrack (24.9 MB generated) | 1 |
| `clawd-on-desk/.github/workflows/*` | move to root or delete (inert) | 1 |
| `autoApproveAllPermissions` boolean | replace with scoped rules | 2 |
| Hook install direct-write | replace with diff + fingerprint | 2 |
| Hook transport fail-closed | replace with ACK + tri-state | 2 |
| Flat memory store | replace with 3-tier + RRF | 2 |
| `CORS allow_origins=["*"]` | replace with token auth | 2 |
| arm64 / macOS / Linux targets | delete (D3) | 1 |
| Vulkan llama-server bundle | delete (installer size) | 1 |
| `desktop/cmd/whatsapp`, wake word, computer use, custom pages, image gen | **never build** | — |

---

## 6. Verification strategy

Standing requirements, not per-phase.

1. **Clean-venv install test in CI** — catches undeclared deps. The exact class that bit `winotify` and was about to bite `reportlab`.
2. **Frozen-exe import assertion** in the gateway build — a declared dep that doesn't import in the frozen binary fails the build.
3. **Contract tests at the two trust boundaries:** hook→app and app→sidecar. `CLAWD_SERVER_HEADER` already exists; extend it to a handshake with a nonce.
4. **Cross-file invariant tests** — the pattern `remote-ssh-deploy.test.js` already uses (parses the shell script's `FILES=()` and asserts set equality with the JS). Extend to: hook event lists, i18n key parity across all 5 locales, tool names in `tool_registry` vs `tools.py` vs the router, `pyproject` vs module-scope imports, `package.json` version vs `pyproject.toml` vs `CHANGELOG.md`.
5. **Kill-switch suite** — for every subsystem: "what happens if the sidecar is dead / the GPU is gone / the network is down / the clock jumps." Several current bugs are exactly this class.
6. **Performance contract**, measured not asserted (from Coucou SPEC §12): hidden = 0 % CPU, compact < 3 %, < 100 MB RSS. Our `low-power` work is the foundation; this makes it falsifiable.
7. **`node --check` sweep** over `src/` — free syntax gate, no config, no new dependency.

---

## 7. Explicitly not building

Paid for in public by VoiceOS, and by Taby's honest roadmap.

| Not building | Why |
|---|---|
| Generic screenshot→click→type computer use | Their changelog is a record of overlay-automation failures. Prefer MCP/API + native-framework actions. |
| Wake word / always-listening | **Both competitors punt.** Trust cliff + battery cost. Genuine white space, wrong time. |
| Custom user pages / sandboxed widgets | Large, speculative, schema-locked forever. |
| Image / video generation | Cloud, or RAM-hungry locally. |
| WhatsApp | Unofficial connection, needs a separate number. |
| Sleeping-computer wake on macOS/Linux | Requires root/admin. Windows-only (D3). |
| Migrating the sidecar to TypeScript | D1. Rewrites working software for no user-visible gain. |

---

## 8. Where this plan is weakest

Stated honestly, because a plan that claims 10/10 isn't a plan.

- **Phase 3 is the risk.** A pluggable engine touches chat, tools, and the sidecar simultaneously. It ships after 0.12.0, behind a feature flag, with `local` as default and the old path still reachable.
- **Phase 2's rules engine is security-critical and under-specified above.** It needs a real threat model before implementation: who is the adversary, what is the blast radius of a wrong rule match? (O2)
- **Nothing here has been executed.** No test run, no build produced. The live-breakage list in §3 comes from reading the user's logs. Phase 0 exists to establish the truth before we build on it.
- **The graphify graph is stale** — built from `592fff6`, and 25 files are modified. Run `graphify update .` before Phase 3.
- **GitHub is not authenticated and no signing cert is present on this machine** (O5). The release hook can be written and unit-tested, but not exercised end-to-end until `gh auth login` and `WIN_CSC_LINK` are set.

---

## 9. Open decisions

| # | Question | Default if unanswered |
|---|---|---|
| O1 | Does 0.12.0 ship with a minimal sound set, or silent? | Ships **with** a minimal generated set — a silent pet is a regression |
| O2 | Written threat model before the rules engine? | **Yes** |
| O3 | Was `v0.11.0` released publicly, and to whom? | Assume yes; the tag moves forward, `v0.11.0` is never rewritten |
| O4 | `clawstick` / Hardware Buddy — revive or delete? | **Delete.** It cannot function without a repo that doesn't exist, and a permanent "Install Clawstick" prompt is worse than absence |
| O5 | `gh auth login` + `WIN_CSC_LINK`? | Unsigned artifacts become **draft** releases, never public |
