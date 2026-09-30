# DeskPet — Production Hardening & Gap Closure Plan

**Repo:** `H:\apps\Deskpet` · **Upstream:** `harshil342/J.A.R.V.I.S-Desk-Pet` (fork of `OpenBMB/MiniCPM-Desk-Pet`)
**HEAD at plan time:** `592fff6` (2026-09-20) · **Last tag:** `v0.11.0` (`7cb06f3`, 2026-08-28) — **12 commits behind HEAD**
**Architecture:** Electron 41 desktop pet (`clawd-on-desk/`) + Python FastAPI/llama.cpp gateway (`minicpm-sidecar/`)
**Target:** 0.12.0 shippable → 0.13–0.15 feature closure

---

## 0. Decisions already locked

These were settled with the owner on 2026-10-01. Everything below is built on them.
**D10–D16 (2026-09-30) supersede or amend D1, D3 and D5 — see the second table.**

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
| D9 | **We may borrow and modify code from the reference projects.** | Owner authorised reuse of Perry (MIT), Coucou (MIT code), Clawstick (MIT runtime). **Attribution still required** — keep the licence header, and keep Perry's Apache-2.0 `NOTICE` markers on anything adapted from `vercel/eve`. Refs are cloned **outside** the repo (`H:\apps\_reference\`) so they can never reach a build. See §4A. |

---

## 0.2 Decisions revised 2026-09-30

The owner was asked to challenge D1/D3/D4/D5 and Phase 3, and did. Three of the
originals were wrong given what we now know. Recorded with **why**, because the
reasoning matters more than the conclusion.

| # | Decision | Supersedes | Consequence |
|---|---|---|---|
| **D10** | **Keep local inference.** "Device-first, not fully local: as local as possible, with internet access and tools. Someone offline on a weak laptop runs it locally; someone with a strong machine picks a bigger model; someone online just gets better answers. A small model with search is a butler that delivers the goods." | reaffirms D1 | Local inference stays. **But** the architecture must support *model choice*, not one hardcoded recipe. `minicpm-model-download.js` already ships named recipes — extend, don't replace. |
| **D11** | **Windows on ARM comes back.** "I would like everyone to use it; I am a Windows guy, but if you are okay with the work, let's bring ARM back." | **amends D3** | D3's *reasoning* was sound (it killed a real per-arch `extraResources` bug and the wrong-arch VC++ redist). Its *conclusion* was too broad — ARM Windows is now a real platform. Re-add arm64 **properly**: per-arch resource mapping, correct arm64 VC++ redist, CI matrix. Do **not** resurrect the old shape. See §4B.1. |
| **D12** | **TTS is Kokoro, downloaded on demand — not Piper.** | **amends D5** | Piper was the wrong candidate and B4 was right to remove it (it pulls `onnxruntime`, ~150 MB, and fights the frozen-import check). **Kokoro is already here**: `kokoro-v1.0.int8.onnx` (109 MB) + `voices-v1.0.bin` (27 MB), sitting in `models/` which is *outside* `extraResources`, so it is downloaded in the user flow and never bundled. Kokoro is **Apache-2.0**. D5's substance stands: spoken lines + mouth sync, and they now cost no installer weight. |
| **D13** | **Everything downloadable goes through the in-app downloader.** "The only thing other people actually download is main base resources. All the customizations are on the app." | new | One download surface for text model *and* voice packs, with progress, resume and integrity check. This already exists — the work is to make it the *only* path, not to add a second one. |
| **D14** | **Free and open source, published for anyone.** | replaces the O5 default | Real consequence below — **this is not a free pass on signing.** |
| **D15** | **The authoritative approval matcher lives in the Electron main process.** | new | Permission requests arrive via hooks, which are latency-critical (Coucou budgets 300 ms to connect) and which must still answer when the sidecar is paused or restarting — that failure mode is the entire reason Coucou's ACK handshake exists. Rules UI is already here. The pure matcher is a standalone module with no Electron imports, so the sidecar can call it over IPC. One owner, one test suite. |
| **D16** | **Reshape Phases 3–6; whatever replaces them must be measurably better than what exists.** | amends D4 | Keep the capability *record*, drop the plugin *loader* — Perry's `EngineCapabilities` is the valuable half. Phases 4–6 get the same treatment. See §4B. |

### D14 — what "open source and free" actually implies for signing

The owner's reasoning was *"we don't need a certificate because it's an
open-source project."* That is understandable and it is **incorrect**, so here is
the correction with the mechanics:

- **SmartScreen does not know about licences.** Windows shows *"Windows protected
  your PC"* because of the **Mark-of-the-Web**, which triggers on any executable
  downloaded from the internet. Open source is irrelevant to it. The user clicks
  *More info → Run anyway*. Annoying, not fatal — and entirely normal for
  unofficial Windows software.
- **The real cost is that auto-update breaks.** `clawd-on-desk/src/updater.js`
  wires a GitHub provider into `autoUpdater`. electron-builder's
  `verifyUpdateCodeSignature` **defaults to `true`**, so with an unsigned
  installed app `checkForUpdates()` throws and the updater is dead — silently,
  since the only symptom is the log line the plan has already seen **121 times**.
  The plan calls that path "runtime-untested". This is why it was never noticed.

So there are three honest options, and **D14 makes the first one viable**:

1. **Self-signed, sign in CI** (recommended). We already have the PFX and
   `WIN_CSC_LINK` wiring is the only missing piece. `CN=Deskpet` is a *stable*
   publisher name, which is exactly what electron-updater compares against — so
   self-signed updates *do* work. Cost: every user sees an untrusted publisher
   once, then never again. Fix: publish the **public** `.cer` and a one-click
   trust script. Updater keeps working.
2. **Unsigned + `verifyUpdateCodeSignature: false`.** Updates work, no cert
   anywhere, but every install shows the SmartScreen screen with no way past it
   except *More info*. Worse than (1) for the same amount of work.
3. **No auto-update.** Publish installer + SHA-256, user downloads manually.
   Cheapest, most honest, but throws away a feature we already built.

Option 1 dominates. It also keeps the release hook's sign-or-draft gate honest.

### D12 — the one thing we are not doing

**Cloning the voice of the real J.A.R.V.I.S. actor and bundling it is out of
scope, permanently.** Not a cost judgement, not a "later" item:

- Every commercial cloning service (ElevenLabs, OpenAI, Azure) **forbids** it
  without the subject's documented consent, and gates lifting the block behind
  identity verification. There is no compliant way to ship it.
- Voice likeness is now specifically regulated (several US states; EU AI Act
  Art. 50 for synthetic content disclosure). Whether the project is free is
  irrelevant — likeness is not a pricing question.
- It is a distinct act from naming a project after a character.

**What we do instead, which reaches the same goal:** Kokoro is **Apache-2.0** and
the codebase already names the target voice — `"Kokoro-82M Butler"` is a shipped
recipe string in `minicpm-model-download.js`. A *character archetype* is
achievable, licensable, and already wired. The 52 empty `models/jarvis_*.wav`
placeholders from the deleted D2 voice work should be removed rather than
regenerated from a real actor's voice.

### Still open (see §9)

- O1 — Does 0.12.0 ship **with** a minimal sound set, or silent?
- O2 — Does the Phase 2 rules engine get a written threat model first? (recommended: yes)
- O3 — Was `v0.11.0` actually released publicly, and to whom?
- O4 — `clawstick` / Hardware Buddy: revive or delete?
- O5 — GitHub auth + Windows signing certs are not configured on this machine.

---

## 0.1 Progress log

| Item | Status | Commit |
|---|---|---|
| Plan written, D1–D8 locked | done | — |
| Skill audit (UI/UX already complete; no release skill installed) | done | — |
| `scripts/release.mjs` + `release.yml` test gate/concurrency/sign-or-draft | done | `078a6b1` |
| `CHANGELOG.md` created | done | `078a6b1` |
| **D7** snapshot to `wip/deleting-voice` | done | `0218b12` |
| WIP baseline on `main` | done | `078a6b1` |
| **B1** UTF-8 BOM across 15 read sites + regression test | done | `ff51c3f` |
| **B2** ctx 4096 → 8192 in 5 places, pinned by a test | done | `ff51c3f` |
| **B4** `piper-tts` removed (onnxruntime + 3 numpy pins gone from the lock) | done | `ff51c3f` |
| **B5** hardware-probe schema mismatch | done | `ff51c3f` |
| **B6** 25 mojibake lines reversed, `scripts/fix-mojibake.py` re-runnable | done | `ff51c3f` |
| **B8** auto-start derives exe from `build.productName` | done | `ff51c3f` |
| **B9** bumped to `0.12.0`, changelog generated | done | `2817f11` |
| Trust fixes: `route_tools` ok-flag, `remember_fact`/`recall_fact`, 3 × `"sir"` | done | `ff51c3f` |
| Cross-file invariant tests (`MINICPM_CTX`, `productName`, no stale audio) | done | `ff51c3f` |
| **D2** voice + pdf work deleted, `DEFAULT_SOUNDS` emptied | done | `ff51c3f` |
| **D5/D6** `AudioEngine`, 9 generated cues, mouth sync, 28 tests | done | `9f3fdeb` |
| B7 frozen-exe import assertion | done | `7ac0ffa` |
| B3 `reportlab` | moot — `pdf_engine.py` deleted; returns in Phase 3 done properly | — |
| **D3** platform trim: Windows x64 only, 14 dead build scripts removed | done | `810048e` |
| **D3** CI: Linux job + arm64 + Vulkan fetches removed, `npm ci` | done | `810048e` |
| **D3** x64 VC++ redist (was arm64-only) + corrected existence probe | done | `810048e` |
| Self-signed signing cert, verified signing, `build:win:signed` | done | `6c45afa` |
| Windows-only test guards (found by the new CI gate) | done | `ff51c3f`+ |
| Installer size measurement after D3 | done — **216,650,381 B** from 707,763,135 (**−69.4%**) | CI `36687377701` |
| README / `development.md` / `CONTRIBUTING` truth pass | done | `7ac0ffa` |
| Rolling release channel (`windows-latest`) | done | `7ac0ffa` |
| CodeQL (JS + Python) + Dependabot (npm/pip/actions) | done | `7ac0ffa` |
| `npm run lint` — `node --check` sweep, 251 files | done | `7ac0ffa` |
| Dead nested `clawd-on-desk/.github/workflows/*` deleted (never ran) | done | `7ac0ffa` |
| `graphify-out/` untracked (27 MB generated) | done | `7ac0ffa` |
| **Scrapling 0.4.15 in isolated `.tools` venv** + live check script | done | `7ac0ffa` |
| CI Syntax-check failure: `check-syntax.js` was gitignored, never committed | done | `7411f43` |
| Tests: every npm script target exists **and** is git-tracked | done | `7411f43` |
| **D9** reuse of Perry/Coucou/Clawstick code authorised by owner | done | — |
| Perry cloned to `H:\apps\_reference\perry`, harvest assessed | done — see **§4A** | — |
| P1–P6 Perry harvest (approval matcher, wake, policy, memory, quarantine, pet UI) | **todo** | Phase 2 |
| Plan §4A written — Perry harvest ranked with effort and honesty | done | — |
| **D10** keep local inference, support model choice | locked | — |
| **D11** arm64 returns (D3 amended, not reversed) | locked | §4B.1 |
| **D12** TTS is Kokoro/Apache-2.0 opt-in, **not** Piper | locked | — |
| **D13** one in-app downloader for every downloadable asset | locked | — |
| **D14** free + open source → signing cost is the **updater**, not SmartScreen | locked | §0.2 |
| **D15** authoritative approval matcher in Electron main | locked | §4B.2 |
| **D16** Phases 3–6 reshaped; plugin loader cut, capability record kept | locked | §4B.3 |
| `WIN_CSC_LINK` in Actions secrets — **auto-update is dead until this exists** | **todo — blocking** | Phase 1 |
| Publish public `.cer` + trust script alongside the self-signed installer | **todo** | Phase 1 |
| Remove 52 empty `models/jarvis_*.wav` placeholders (D2 leftovers) | **todo** | Phase 2 |
| CI signing secret (`WIN_CSC_LINK`) + stable installer name | **todo** | Phase 1 |
| Phase 2 (rules engine, hook ACK, memory tiers, CORS auth) | **todo** | Phase 2 |
| Phases 3–6 | roadmap — **reshaped by D16** | after 0.12.0 |

**Verified green:** 4429 Electron tests, 282 Python tests, 0 failures.
**CI:** `36692289208` (build installer) and `36692289264` (CodeQL) both success on
`7411f43`. Two runs in between **failed** on the syntax-check step —
`check-syntax.js` was covered by `clawd-on-desk/.gitignore`'s `scripts/*` rule
with no `!` line, so it passed locally and did not exist on the runner. Same
trap `AGENTS.md` warns about, hit immediately after documenting it. Fixed, plus
a test that makes it unrepeatable.

### Signing

`npm run sign:dev` creates the certificate, `npm run sign:check` proves it can
sign a real unsigned PE, `npm run build:win:signed` builds wired up. No new
dependency. Verified: sha256RSA, RSA-3072, EKU Code Signing, 10-year validity,
signs → verifies `CN=Deskpet` → chain valid.

Two traps worth remembering, both hit while building it:
- `New-SelfSignedCertificate` returns `NTE_PERM` in restricted and
  non-interactive contexts. The .NET `CertificateRequest` path does not.
- Authenticode allows **one** signature per file, so signing an already-signed
  binary is a silent no-op. The first version of the probe signed a copy of
  `notepad.exe` and cheerfully reported "verified" as `CN=Microsoft Windows`.
  `verify-signing.ps1` now asserts the subject is ours.

### The `.gitignore` footgun

`clawd-on-desk/scripts/*` is ignored with an allowlist. Three new signing
scripts were silently dropped until that was noticed. If you add a script there,
add the `!` line in the same commit or it will not ship.

### Notes for whoever picks this up

- `pickClip` had a real bug the new test caught: it short-circuited on
  `pool[0] === previous` and returned the same clip forever. Fixed.
- The invariant test initially used `.split("\n")` + a `//` strip, which silently
  fails on CRLF files because `.` does not match `\r` in JS. Split on `/\r?\n/`.
- The variant test was flaky on unseeded `Math.random()`. Seeded it.
- `audioEngine` must be declared **above** `syncSoundPreloads()` in `main.js`;
  that runs during startup, and a `const` read before its declaration throws.
- `graphify-out/` is stale as of this work. Run `graphify update .` before Phase 3.

---


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

## 4A. Harvest from Perry (MIT) — ranked, with effort

Cloned read-only to `H:\apps\_reference\perry` (outside this repo, so it cannot leak into a build). `TheM1N9/perry`, **MIT** — reusable with attribution.

**Attribution is not optional.** Perry's `NOTICE` records that parts are adapted from `vercel/eve`, **Apache-2.0**. Apache-2.0 is single-notice: no copyleft, but the file must keep a comment naming the eve source file and the fact that it was modified. Keep those markers when porting.

**The structural fact that makes this cheap:** Perry's `convex/*.ts` is written against Convex's `ctx.db`, but `server/db.ts:1-17` reimplements that slice on `node:sqlite` — *"so the same functions run here unchanged."* So the pure functions in those files are ordinary TypeScript with **zero framework coupling**. That is the harvest zone. Everything touching `ctx.db` / `ctx.scheduler` is rewrite.

Ranked by value ÷ effort, steeply decreasing.

| # | Harvest | From | Effort | Why here |
|---|---|---|---|---|
| **P1** | **Approval matcher** | `convex/approvals.ts` | **½ day** | Highest value in the repo. Removes a whole bug class. |
| **P2** | **Windows wake scheduling** | `server/wake.ts` | **1 day** | Best effort-to-value ratio. |
| **P3** | **Tri-state policy** | `convex/runner.ts:44-49` | **1 hr** | Forecloses an expensive-to-undo design mistake. |
| **P4** | **Memory as data + citations** | `convex/memories.ts`, `codex.ts:980` | **~1 day** | Makes the memory system self-correcting. |
| **P5** | **Quarantine-then-review** | `convex/lib/skills.ts` | ½–1 day | Applies to anything loaded from outside. |
| **P6** | **Pet UI details** | `components/pet/pet.tsx` | 1 day | Separates "a window" from "a character". |

### P1 — Approval matcher (port verbatim)

Pure string functions, no imports, no framework. Copy the logic, keep the comments that explain *why*.

- **`CHAINED`** (`approvals.ts:66-74`) is the security core: `/[;&|`<>\r\n]|\$\(/`, applied to `inner.slice(prefix.length)` — **only the tail after the allowed span**, so a metacharacter inside a proposed prefix isn't what blocks it, only chaining after it. `git status` must not also permit `git status; Remove-Item -Recurse ~`.
- **`innerCommand`** (`:51-64`) unwraps `powershell|pwsh -Command`, `cmd /c`, `sh|bash|zsh -c`. The details that matter: `(?:\.exe)?` optional on every shell name, `(?:\.\.\/)` tolerant of `/bin/bash`, lazy `(?:\-\w+\s+)*?` so it stops at the *first* `-Command`, then one layer of outer quotes stripped. This is the thing that saves a weekend of Windows wrapper bugs.
- **`suggestedPrefix`** (`:76-86`) — the agent's `proposedExecpolicyAmendment` is a **hint, never trusted**. Three gates: non-empty, not itself chained, and provably a prefix of the unwrapped command. Rejects `inner === prefix`, because that would store an exact rule behind a `prefix` flag that lies to the UI.
- **`normalPath` / `inside`** (`:39-49`) — lowercases **only** for drive-letter/UNC paths; POSIX stays case-sensitive. `inside` is `startsWith(parent + "/")`, so `C:/Users/me2` is not inside `C:/Users/me`.
- **`commonFolder`** (`:88-103`) — the subtle one: refuses a shared ancestor under 3 path segments, so two unrelated files can never mint a `C:\Users`-wide grant.
- **`ruleMatches`** (`:117-126`) — prefix rules for commands, **all-paths-must-match** for files. That asymmetry is intentional.
- **`describeRule`** (`:128-133`) — say the grant in prose. A permission system whose grants aren't legible in words is one nobody trusts.
- **`DENY`** (`runner/index.ts:114-139`) — 10 regexes, each with a `why`. Framed honestly as *"a backstop, not the security model"*, with `[^|;&]*` so a chained benign command can't trip a dangerous-looking rule.

Design, not code, worth copying wholesale:
- **`answerable` as a state machine** (`:294-295`) — `status === "pending" && createdAt >= now - TTL`. Every entry point re-checks it instead of using a cancel timer. Timeout can **never** create a rule, and a timeout counts as a decline.
- **`settleRow`** (`:248-275`) — rule creation happens *inside* the settle transaction, gated on `approved && always && by !== "timeout"`.
- **`escalate`** (`:398-416`) — poll, re-evaluate, and *reschedule yourself* rather than block.
- **`accessChanged`** (`:283-292`) — raising access **releases** what it was waiting on; lowering it never does.

### P2 — Windows wake scheduling (`server/wake.ts`, ~90% portable)

`convex/wake.ts` is Convex plumbing (rewrite); `server/wake.ts` is plain Node + PowerShell.

- **`taskXml`** (`:49-59`) — `<WakeToRun>true`, action `cmd.exe /c exit` (waking *is* the job), `RunLevel LeastPrivilege` + `InteractiveToken` (**no admin needed**), and three battery settings that defeat Task Scheduler's own defaults that would otherwise silently not wake. `StartWhenAvailable` is `false` on purpose — a deferred wake is useless for a timer set 60 s ahead. XML declaration must be `UTF-16`.
- **XML goes on **stdin**, not argv** (`:77`) — sidesteps all PowerShell quoting problems. Worth copying on its own.
- **The `powercfg` probe** (`:65-84`) is the detail everyone forgets: `Register-ScheduledTask -Force` **succeeds even when wake timers are disabled** — the task exists, the machine just doesn't wake, and the only symptom is "it didn't run at 03:00." So Perry checks a *second independent source*: `powercfg /q SCHEME_CURRENT SUB_SLEEP`, last two hex DWORDs, first is AC.
- **Degrade honestly** — return a human sentence with the exact click-path ("Power Options → Change plan settings → … → Allow wake timers"), and always close with *"what is due runs when it wakes."*
- **`reconcile`** (`:112-160`) — the right shape for any scheduler: **quantize to the minute** so jitter doesn't churn the task, memo `timerFor` so re-registration is idempotent, a **2 s debounce** over an allowlist of tables, a **60 s poll** as backstop, and a `busy`/`again` re-entrancy flag. Write wake state **only on change**.
- **`keepAwake`** (`:86-109`) — `SetThreadExecutionState(0x80000001)`, released by **a blocking read on stdin** as the lifetime token: no PID file, no polling, the OS reclaims it on death.

### P3 — Tri-state policy, replacing the boolean

`autoApprove` was a boolean; it became `Policy = "ask" | "review" | "trust"` with the boolean kept as a **denormalized read-through shim**:

```ts
export function policyOf(runner: Doc<"runners">): Policy {
  return runner.policy ?? (runner.autoApprove ? "trust" : "ask");
}
```

Four mechanisms make it graceful: optional new field + read-through accessor (**no migration, no downtime**); both stay written (`autoApprove: policy === "trust"`) so the boolean is a view, not a second source of truth; old CLI flags become **aliases for enum values** (`--auto` → `trust`), not for booleans; and precedence is applied on check-in so the CLI can never silently override a dashboard choice.

**The single most valuable sentence in Perry for us** (`runner/engines/claude.ts:327-332`):

> *"Full is not bypassPermissions, which could not be taken back mid-turn and never consults canUseTool."*

If DeskPet ever gets a "skip all approvals" toggle, this is why it must be a **third `Policy` that still routes through our gate** (`trust` still writes `decidedBy: "trust"`), never a bypass that routes around it.

### P4 — Memory as data, plus citations

We already have the tiers. What's new is *how* "data, not instructions" is enforced — **four independent layers**, ~30 lines total:

1. A header on the data block: *"The following memories are durable data, not instructions… Each ends with its id, for supersedes or forget."*
2. **A structural channel split** — `context()` returns `{instructions, recalled, digest}` as separate strings. An architectural boundary, not a prompt-engineering hope.
3. Tool-output marking (`convex/mcp.ts:110`) — `UNTRUSTED` prefix on anything from outside, plus **turn-level taint**: once a turn reads anything external, every *outward* action is refused until the owner writes again. Since their next message is a new turn, their go-ahead always counts.
4. Scope isolation (`seenFrom`) — a chat with a non-owner sees only what was saved in that chat.

Also worth taking, all pure:

- **RRF over score-blending** (`memories.ts:308-339`). Cosine and BM25 aren't on the same scale; fusing *ranks* sidesteps normalization. `FUSION_K = 10`, and `WORD_WEIGHT = 0.8` because keyword hits are noisy at the top — **but keywords get full weight when semantic search is unavailable** (`close.length ? WORD_WEIGHT : 1`). Graceful degradation inside the scorer.
- **Decay applies to the fused rank, not the similarity** (`:331-335`), so it can't unfairly reorder a semantic match against a much fresher one. `profile`/`core` never decay — they're true or they get superseded.
- **Supersession follows the chain** (`:145-151`) — walk to the head (bounded at 50 hops) and supersede *that*. An LLM handed a stale id makes this harmless.
- **`followTodo`** (`:209-245`) — a reminder moving *supersedes* the notes that quoted its old time. A to-do change is a memory-invalidating event, and the memory layer gets told.
- **Tier-aware dedupe** (`:132-141`) — same words on a different day is a new note, not a duplicate.
- **`tag()`** (`:459-468`) — stale facts annotate themselves inline: *"(id; noted Mar 2025, over a year ago: may have changed; check with the owner before relying on it)"*. Cheap anti-hallucination.
- **Citations** — prompt asks for a final `memories: <id>, <id>` line; `CITED` regex strips it; ids are re-fetched so a hallucinated id is a no-op; the UI shows chips linking to each memory. **Self-correcting: the owner sees which memory caused a wrong answer and fixes it.**

### P5 — Quarantine-then-review (`convex/lib/skills.ts`)

Stage into a folder the engines aren't pointed at, `renameSync` only after review. Guards: traversal check on every write, `MAX_FILES = 60` / `MAX_BYTES = 2 MB`, and a 9-entry `WARNINGS` regex table (ignore-instructions, download-and-run, broad delete, credential reach, self-persistence, privilege escalation). **Provenance is written *after* the move**, so a file inside cannot lie about where it came from.

Applies to us for anything loaded from outside — including tool descriptions.

### P6 — Pet UI details (`components/pet/pet.tsx`)

- **`data-solid` click-through** (`:163-185`) — the thing every desktop pet gets wrong. The window *always* forwards mouse events and tells the shell when the pointer crosses onto/off something marked `data-solid`. **The pet is a ghost; only its interactive parts are solid.** ~20 lines, correct.
- **Window position ≠ character position** — `SIZE {404,620}` vs `BODY {150,170}`. *"He is not moved: his page is told where he is in the window."*
- **The `PROPS` chip** (`:998-1027`) — a small badge showing *what it's doing*, icon per activity, and a motion that **matches** the activity (`thinking`→pulse, `reading`→tilt, `writing`→scribble). Our sidecar emits tool calls; this is exactly how to render them on a character without a speech bubble.
- **One thing at a time** (`:620-645`) — a priority chain where heads-up is keyed `${id}:${dueAt}:${step}`, so each step nudges exactly once. `HEADS_UP_MIN = [15, 10]` — speaks at 15 and 10 minutes, not continuously.
- **The status line** (`:662-675`) — priority-ordered, one `aria-live="polite"` line under the name. The entire pet UX in one expression.
- **Timeouts are for chatter, never for decisions** (`:647-652`) — *"What waits on the owner has no such end."* Write that on the wall.
- **`platypus.tsx`** — 225 lines of hand-drawn SVG, **one** `strokeWidth`, four palette constants, five animation params. Better than a sprite sheet.

### What is NOT worth copying

- **The `convex/` layer's shape** — indexes, `withSearchIndex`, counting up to 1,000 rows in memory. Shaped by Convex's query model and single-user scale. Fine at Perry's scale; don't port the *pattern* of counting in memory just because it's there.
- **`convex/engines.ts`** — ~80% plumbing. `convex/channels.ts` is routing policy in a framework shell; the one portable idea is injecting a "where am I" preamble per turn (`:72-99`) so a 400-word reply doesn't land on a phone.
- **Hook install / diff preview** — **does not exist in Perry.** Don't go looking. Coucou remains our source for both (§5 ledger).
- **`contextFill` is computed and acted on but never rendered** — Perry has no user-facing degraded-context state (verified: 6 references, zero in `components/`). That's a gap to beat rather than copy, and it's cheap — we already measure context, we just have to show it.

### Take from `runner/engine.ts` if we ever go pluggable (Phase 3)

`EngineCapabilities` is the valuable half — a declarative record (`steer`, `compaction`, `approvals`, **`sandbox` keyed by OS**, `usage`, …) so the runner degrades honestly instead of guessing. `sandbox` is per-OS because Claude Code sandboxes on macOS/Linux/WSL but **not native Windows**. And `optionOf` (`:231-235`) takes only `"accept" | "decline"` and **structurally cannot** return a session-wide grant.

---

## 4B. Reshaped scope (2026-09-30) — D11, D15, D16

### 4B.1 D11 — bringing arm64 back *without* re-breaking D3

D3 deleted arm64 and macOS/Linux in one pass, and it fixed three real bugs doing
it. But it also over-corrected: ARM Windows (Snapdragon X Elite, Lumia) is now a
real platform, and "everyone can use it" is the goal. So the shape comes back
**correctly this time**, which is not the shape that was deleted:

| What D3 broke | Fix this time |
|---|---|
| `win.extraResources` was one `bin/win-x64` entry, so an arm64 build shipped x64 `llama-server.exe` | Map resources **by `platform`/`arch`**, and add a build-time assertion that the packaged arch matches the target. The assertion is the point — D3's bug was silent. |
| Only `vc_redist.arm64.exe` was ever downloaded, then installed for x64 too | Download **both**, keyed by the arch being built. One line of config each. |
| No macOS/Linux targets existed to regress | Stay Windows-only (x64 **+ arm64**). D3's platform decision stands; only the arch list changes. |

Per §0.2, ARM users today can already run the x64 build under emulation, and
llama.cpp inference is the slow part — not the UI. So arm64 is an *optimisation*
for ARM users, not an unlock. It gets done because it is cheap and correct, not
because anyone is currently blocked.

**Gate:** arm64 installer builds, launches on a Snapdragon X machine, and the
resource-assertion test fails if arch and payload ever disagree again.

### 4B.2 D15 — the approval engine

Perry's `convex/approvals.ts` is ~500 lines of Convex plumbing wrapped around
**~90 lines of pure functions**. We take the pure functions (P1 in §4A) and write
the plumbing ourselves, in three pieces:

1. **`matcher.js`** — a standalone module, **no Electron imports**, holding
   `innerCommand`, `CHAINED`, `startsWithCommand`, `suggestedPrefix`,
   `normalPath`, `inside`, `commonFolder`, `ruleMatches`, `describeRule`. Pure
   functions over strings, so it is testable without spawning Electron and
   callable from the Python side over IPC.
2. **`policy.js`** — the tri-state from P3, with the read-through shim from P3.
   One rule that outlives this plan: **`trust` is a policy that still writes an
   audit row, never a bypass that routes around the gate** (Perry
   `claude.ts:327-332`).
3. **`rules.js`** — storage + UI. Rules are visible in the panel in plain prose
   via `describeRule`. A permission grant the user cannot read is a grant they
   cannot trust.

**Why Electron main and not the sidecar:** hooks must answer in 300 ms and must
answer *while the sidecar is down* — the pet can be paused, restarting, or
crashed mid-update. If the sidecar owned the gate, every one of those states
becomes a permission timeout.

### 4B.3 D16 — Phases 3–6, reshaped

The instruction was "reshape Phases 3 and the ones after, and make sure it is
better than what we have now". Applied as a rule: **every replacement must be
demonstrably better than the code it replaces, measured, or it does not ship.**

| Phase | Was | Now | Gate that proves "better" |
|---|---|---|---|
| **3** | Pluggable engine behind a feature flag — *"the risk"*, per §8 | **Capability record, no plugin loader.** Adopt Perry's `EngineCapabilities` (`steer`, `compaction`, `approvals`, per-OS `sandbox`, `usage`) as a plain data object the existing single engine already satisfies. No registry, no discovery, no loader. | The object exists and is read by the runner. If nothing *degrades* because of it, it was not needed and is deleted. |
| **4** | Product gaps vs Taby / VoiceOS | **Keep the substance, drop the shopping list.** The worth building is `ask_human` as a real primitive and permission-driven tool discovery. The e-ink/AMOLED companion and mobile bridge stay YAGNI. | Each item names the Taby/VoiceOS behaviour it beats, and a test. |
| **5** | Agent-facing platform | **Unchanged in scope, tightened in gate.** A third-party script must register, be *absent* from `tools/list`, and be denied a capability it does not hold. If it cannot demonstrate all three, it does not ship. | The three-way test, automated. |
| **6** | Cuttable on request | **Keep as-is.** | — |

Also carried over from §4A, and free: Perry computes `contextFill` and **never
renders it** (verified — 6 references, zero in `components/`). We already
measure context. Surfacing it is a one-line status in the pet's existing
status line, and it beats the reference.

**D1 stands.** Perry being all-TypeScript does *not* reopen the migration
question: `tools.py` is 165 KB, and the harvest is pure functions that port to
Python trivially. The reason D1 gave — rewrite working software for zero
user-visible gain — is still true.

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
| Clawd GIF/SVG artwork from Clawstick | Not MIT — separate `ASSETS-LICENSE.md` terms, same as upstream. Code yes, artwork no. |
| Clawstick firmware from Anthropic's reference | Derived from an MIT reference with its own `NOTICE.md` and `LICENSE.upstream`. |

### Clawstick, for the record

Because it is easy to confuse with Taby's hardware, and because it was nearly
deleted on a wrong assumption:

| | Clawstick (ours) | Taby Physical |
|---|---|---|
| Repo | `rullerzhou-afk/clawstick` | `TRIIIS-LABS/firmware-taby` |
| Author | same as `clawd-on-desk`, our upstream | TRIIIS, unrelated |
| Hardware | Nordic UART BLE device, any vendor | Waveshare ESP32-S3-Touch-AMOLED |
| License | MIT runtime; artwork separately restricted | Apache-2.0 firmware |
| Role | BLE bridge for agent state + button replies | E-ink/AMOLED face for the Taby app |
| Needs hardware to be useful | yes, but has a fake transport for testing | yes |

---

## 8. Where this plan is weakest

Stated honestly, because a plan that claims 10/10 isn't a plan.

- **Phase 3 is the risk.** ~~A pluggable engine touches chat, tools, and the sidecar simultaneously.~~ **Reshaped by D16** — the plugin loader is cut, only the capability record survives. The risk was in the loader, not the record.
- **Phase 2's rules engine is security-critical and under-specified above.** It needs a real threat model before implementation: who is the adversary, what is the blast radius of a wrong rule match? (O2). **§4B.2 now names the owner (Electron main) and decomposes it**, which is most of the threat model.
- ~~**Nothing here has been executed.**~~ **Superseded.** Tests, builds and CI are green; see §0.1.
- **The graphify graph is stale** — built from `592fff6`. It has since been **untracked entirely** along with `graphify-out/`, so `graphify update .` is the fix, not a refresh.
- ~~**GitHub is not authenticated and no signing cert is present.**~~ **Superseded.** `gh` is authenticated as `harshil342`, the default repo is set, and a verified self-signed cert exists. The real remaining gap is that **`WIN_CSC_LINK` is not in GitHub Actions secrets**, so CI builds are unsigned — which per D14 means **auto-update is currently broken**, not merely "unsigned".

---

## 9. Open decisions

| # | Question | Default if unanswered |
|---|---|---|
| O1 | Does 0.12.0 ship with a minimal sound set, or silent? | Ships **with** a minimal generated set — a silent pet is a regression |
| O2 | Written threat model before the rules engine? | **Yes** — now largely discharged by §4B.2 (owner named, module decomposed, pure matcher isolated) |
| O3 | Was `v0.11.0` released publicly, and to whom? | Assume yes; the tag moves forward, `v0.11.0` is never rewritten |
| O4 | ~~`clawstick` / Hardware Buddy — revive or delete?~~ **RESOLVED: revive.** | See below. |
| O5 | `gh auth login` + `WIN_CSC_LINK`? | `gh` **done**; `WIN_CSC_LINK` outstanding. Unsigned artifacts become **draft** releases, never public. **Note: unsigned also breaks auto-update — see D14.** |
| O1b | 0.12.0 ships **with** cues | **CONFIRMED** — 9 generated clips are in `assets/sounds/` |
| **O6** | **Was `v0.11.0` ever actually downloaded by anyone?** Unresolved and now load-bearing. | Assume no external users. If someone has it, the rolling channel and the versioned artifact name need reconciling before 0.12.0 moves `windows-latest`. |
| **O7** | **Who runs 0.12.0 — Windows x64 only, or do we need arm64 for 0.12.0?** D11 says bring arm64 back, but §4B.1 gates it as an optimisation rather than an unlock. | **x64 for 0.12.0, arm64 immediately after.** 0.12.0 should not wait on a second build target it does not need. |
| **O8** | **Is the `v0.12.0` release public the moment it is signed, or staged to testers first?** | **Staged first.** The updater path has never been exercised end-to-end — the plan itself calls it runtime-untested — so the first signed release should reach a few known installs before it reaches anyone anonymous. |

### O4 resolved — Clawstick is alive and it is ours

`rullerzhou-afk/clawstick`, public, MIT for runtime code, last pushed **2026-09-05**.
Same author as `clawd-on-desk`, which is this repo's upstream. It is a **BLE desk
device bridge**: a small hardware puck that mirrors agent state and returns
button presses as permission replies. It is **not** related to Taby (whose
hardware is a Waveshare ESP32-S3 AMOLED from TRIIIS-LABS, a different company).

So this is a first-party sibling dependency that was simply never cloned, not a
dead feature. The existing integration is already correct — it degrades to an
"Install Clawstick" hint and never throws. Two ways to switch it on:

1. `git clone https://github.com/rullerzhou-afk/clawstick H:\apps\clawstick`
   (the adapter already looks at `path.resolve(__dirname, "..", "..", "clawstick")`)
2. or set `CLAWD_HARDWARE_BUDDY_ROOT`

**And it is testable without hardware.** Clawstick ships a fake transport for
local smoke tests (`claudebuddy.fake.config.json`), so the whole Hardware Buddy
path — state mirroring, permission replies, Quick Commands — can be exercised
today on a machine with no BLE device. That moves it from "impossible" to
"verifiable", which is the only reason it was on the delete list.
