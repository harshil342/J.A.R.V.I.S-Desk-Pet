"use strict";
//
// MiniCPM Chat — a single bubble window that lives next to the pet and acts
// like a speech / thought balloon. Click pet → input bubble pops up; press
// Enter → bubble vanishes while the pet does its thinking animation; once the
// model starts replying, the bubble reappears with the streamed text and
// fades out a few seconds after the reply finishes.
//
// The window is created lazily on first open and then *hidden* on dismiss —
// the renderer keeps the in-memory conversation history across opens.
//
// Layout assumption:
//   <repo-root>/clawd-on-desk        ← this Electron app
//   <repo-root>/minicpm-sidecar      ← llama.cpp-backed sidecar
//                                       (gateway/ FastAPI + llama-server)
//   <userData>/models/*.gguf         ← GGUF weights downloaded by Onboarding
//
// Override locations via env:
//   MINICPM_SIDECAR_BIN  — point at a prebuilt gateway binary
//   MINICPM_SIDECAR_DIR  — point at the minicpm-sidecar source tree (dev)
//   MINICPM_PYTHON       — explicit Python interpreter (dev fallback)
//
// Historical note: this used to spawn a PyTorch sidecar via conda / uv.
// That stack was retired in v0.8 in favour of llama.cpp; the new sidecar
// has no torch / transformers / peft dependency and ships as a single
// binary per platform alongside llama-server.

const { BrowserWindow, ipcMain, screen, shell, Menu, app } = require("electron");
const { spawn, execFile } = require("child_process");
const { promisify } = require("util");
const execFileAsync = promisify(execFile);
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const isMac = process.platform === "darwin";
const isWin = process.platform === "win32";
const isLinux = process.platform === "linux";
const WIN_TOPMOST_LEVEL = "pop-up-menu";
const LINUX_WINDOW_TYPE = "splash";

// Port chosen to dodge common collisions on dev machines: 8765 is used
// by Apache CouchDB tests, Bitcoin Cash testnet, and a few other tools.
// 18765 ("1" prefix on the old default) is unassigned by IANA and easy
// to remember. Override via MINICPM_PORT env if you need something else.
const DEFAULT_PORT = 18765;
const DEFAULT_HOST = "127.0.0.1";
const BUBBLE_GAP = 8;   // pixels between visible pet sprite and bubble
const EDGE_MARGIN = 8;

const ASK_WIDTH = 120;       // initial empty-input width — tiny pill
const ASK_HEIGHT = 44;
const SPEAK_MAX_WIDTH = 360;
const SPEAK_MAX_HEIGHT = 360;
const MIN_WIDTH = 100;
const MIN_HEIGHT = 40;


// Sidecar locating/management and history-store helpers live in their own
// modules; every name is re-imported so the eval-based tests that bind
// __internals from this file keep working.
const {
  locateSidecarBinary,
  locateSidecarSourceDir,
  locatePython,
  triplet,
  parseManifestJson,
  manifestUpsertItem,
  manifestRemoveItem,
  adapterMatchesHint,
  planBundledReconcile,
  safeDeleteTargetFor,
  seedAdaptersFromBundle,
  httpJson,
  Sidecar,
} = require("./minicpm-sidecar-manager");
const {
  sanitizeHistoryItems,
  freshHistoryStore,
  capHistoryStore,
  normalizeHistoryStore,
  HISTORY_MAX_MESSAGES,
  HISTORY_MAX_CONTENT_CHARS,
  HISTORY_MAX_SESSIONS,
  HISTORY_DEFAULT_SESSION,
} = require("./minicpm-history-store");


// Assistant prefs consumed by this module + the chat bubble. Defaults
// mirror the clawd-prefs.json schema (src/prefs.js); the snapshot getter
// overlays whatever the controller validated so pre-schema snapshots and
// test stubs still produce a complete projection.
const ASSISTANT_PREF_DEFAULTS = Object.freeze({
  assistantAccent: "#8A939B",
  accentPreset: "custom",
  bubbleOpacity: 0.94,
  bubbleBlur: 28,
  bubbleTextScale: 1,
  bubbleDensity: "comfortable",
  typewriterEnabled: true,
  assistantAddress: "sir",
  briefingHour: 8,
  recapHour: 21,
  reminderChime: true,
  autoMemory: true,
  clarifyStrength: "ambiguous",
});

// Map an assistant-prefs snapshot onto the snake_case body the sidecar
// gateway expects at POST /api/config (gateway/runtime_config.py).
function buildAssistantConfigPayload(prefs) {
  const p = prefs || {};
  return {
    assistant_address: p.assistantAddress,
    clarify_strength: p.clarifyStrength,
    auto_memory: !!p.autoMemory,
    briefing_hour: Number.isInteger(p.briefingHour) ? p.briefingHour : ASSISTANT_PREF_DEFAULTS.briefingHour,
    recap_hour: Number.isInteger(p.recapHour) ? p.recapHour : ASSISTANT_PREF_DEFAULTS.recapHour,
  };
}

// Deduped POSTer for /api/config. The signature is recorded only after
// post() resolves, so a failed push is retried on the next trigger.
function createAssistantConfigSyncer(post) {
  let lastSentSig = null;
  return async function sendAssistantConfig(snapshot) {
    const payload = buildAssistantConfigPayload(snapshot);
    const sig = JSON.stringify(payload);
    if (sig === lastSentSig) return false;
    await post(payload);
    lastSentSig = sig;
    return true;
  };
}


function pickSide(petBounds, workArea, width, height, preferred = "auto") {
  const wb = workArea.x + workArea.width;
  const hb = workArea.y + workArea.height;
  const fitsRight = (petBounds.x + petBounds.width + BUBBLE_GAP + width) <= (wb - EDGE_MARGIN);
  const fitsLeft = (petBounds.x - BUBBLE_GAP - width) >= (workArea.x + EDGE_MARGIN);
  const fitsBelow = (petBounds.y + petBounds.height + BUBBLE_GAP + height) <= (hb - EDGE_MARGIN);
  const fitsAbove = (petBounds.y - BUBBLE_GAP - height) >= (workArea.y + EDGE_MARGIN);
  // Honor the user's preferred side when it fits; fall back to the
  // opposite if there's no room there. "auto" preserves the original
  // right-first ordering for backward compatibility.
  if (preferred === "left") {
    if (fitsLeft) return "left";
    if (fitsRight) return "right";
  } else if (preferred === "right") {
    if (fitsRight) return "right";
    if (fitsLeft) return "left";
  } else {
    if (fitsRight) return "right";
    if (fitsLeft) return "left";
  }
  if (fitsBelow) return "below";
  if (fitsAbove) return "above";
  return preferred === "left" ? "left" : "right";
}

function computeBubbleBoundsForSide(side, petBounds, workArea, width, height, opts = {}) {
  const cx = petBounds.x + petBounds.width / 2;
  const cy = petBounds.y + petBounds.height / 2;
  const wb = workArea.x + workArea.width;
  const hb = workArea.y + workArea.height;
  // verticalAnchor: "center" (default) — bubble grows from middle; "bottom" —
  // bubble's bottom edge stays put as it grows (used during continuous-chat
  // typing so the textarea position stays stable under the cursor).
  const vAnchor = opts.verticalAnchor || "center";
  const anchorBottomY = opts.anchorBottomY;
  // User-saved offsets (from drag-to-position in Settings). dx is signed
  // "further from pet"; dy is signed "downward from pet vertical center".
  const offsetDx = Number.isFinite(opts.offsetDx) ? opts.offsetDx : 0;
  const offsetDy = Number.isFinite(opts.offsetDy) ? opts.offsetDy : 0;

  let x, y;
  if (side === "left" || side === "right") {
    if (side === "left") x = petBounds.x - BUBBLE_GAP - width - offsetDx;
    else                 x = petBounds.x + petBounds.width + BUBBLE_GAP + offsetDx;
    if (vAnchor === "bottom" && Number.isFinite(anchorBottomY)) {
      y = anchorBottomY - height;
    } else {
      y = cy - height / 2 + offsetDy;
    }
  } else if (side === "above") {
    x = cx - width / 2 + offsetDx;
    y = petBounds.y - BUBBLE_GAP - height - offsetDy;
  } else { // below
    x = cx - width / 2 + offsetDx;
    y = petBounds.y + petBounds.height + BUBBLE_GAP + offsetDy;
  }
  x = Math.round(Math.max(workArea.x + EDGE_MARGIN, Math.min(x, wb - EDGE_MARGIN - width)));
  y = Math.round(Math.max(workArea.y + EDGE_MARGIN, Math.min(y, hb - EDGE_MARGIN - height)));
  return { x, y, width: Math.round(width), height: Math.round(height) };
}

// ── Window manager ──────────────────────────────────────────────────────────

module.exports = function initMinicpmChat(ctx) {
  const appRoot = path.resolve(__dirname, "..");
  const sidecarDir = locateSidecarSourceDir(appRoot);
  const sidecarBin = locateSidecarBinary(appRoot);
  const port = Number(process.env.MINICPM_PORT || DEFAULT_PORT);
  const host = process.env.MINICPM_HOST || DEFAULT_HOST;
  const log = (msg) => { try { console.log(msg); } catch {} };

  // ── i18n bridge ──────────────────────────────────────────────────────
  // ctx.getLang() returns the *effective* UI language. Used to translate
  // sidecar errors (raised with a `minicpmI18nKey` annotation) and to
  // provide the chat renderer with its initial dictionary + classifier
  // few-shots over IPC.
  const minicpmI18n = require("./minicpm-i18n");
  const getLang = () => {
    try {
      if (ctx && typeof ctx.getLang === "function") {
        const v = ctx.getLang();
        if (typeof v === "string" && v) return v;
      }
    } catch {}
    return "en";
  };
  const tr = minicpmI18n.makeTranslator(getLang);
  function localizeError(err) {
    if (!err) return "";
    if (err.minicpmI18nKey) {
      return tr(err.minicpmI18nKey, err.minicpmI18nParams || {});
    }
    return err.message || String(err);
  }

  if (sidecarBin) log(`[minicpm-chat] using packaged sidecar binary: ${sidecarBin}`);

  // Resolve <userData>/logs/ once so every consumer can point at the
  // same directory (sidecar stream + crash dumps + Settings "open log
  // folder" button).
  function getLogsDir() {
    try { return path.join(app.getPath("userData"), "logs"); }
    catch { return path.join(os.tmpdir(), "minicpm-logs"); }
  }
  const logsDir = getLogsDir();
  try { fs.mkdirSync(logsDir, { recursive: true }); } catch {}
  const sidecarLogPath = path.join(logsDir, "sidecar.log");

  // Shared MiniCPM prefs path. This must be initialized before any boot-time
  // adapter/model helpers call readMinicpmPrefsRaw().
  const PARAMS_PATH = (() => {
    try { return path.join(app.getPath("userData"), "minicpm-prefs.json"); }
    catch { return path.join(os.tmpdir(), "minicpm-prefs.json"); }
  })();

  // Chat history persistence file. Limits + the sanitizer live at
  // module scope (sanitizeHistoryItems) so tests can exercise them
  // without booting the whole factory.
  const HISTORY_PATH = (() => {
    try { return path.join(app.getPath("userData"), "minicpm-chat-history.json"); }
    catch { return path.join(os.tmpdir(), "minicpm-chat-history.json"); }
  })();

  // ── Assistant prefs projection ────────────────────────────────────────
  // The settings controller owns clawd-prefs.json; main injects a lazy
  // snapshot getter (ctx.getAssistantPrefs). We project the keys this
  // module + the chat bubble consume onto schema defaults so a missing
  // key can never produce an undefined theme/behavior value.
  function getAssistantPrefsSnapshot() {
    const out = { ...ASSISTANT_PREF_DEFAULTS };
    try {
      const snap = ctx && typeof ctx.getAssistantPrefs === "function"
        ? ctx.getAssistantPrefs()
        : null;
      if (snap && typeof snap === "object") {
        for (const key of Object.keys(ASSISTANT_PREF_DEFAULTS)) {
          if (snap[key] !== undefined && snap[key] !== null) out[key] = snap[key];
        }
      }
    } catch {}
    return out;
  }

  // Push address/persona behavior config into the sidecar gateway.
  // Fire-and-forget: silent catch, deduped against the last successfully
  // POSTed payload so repeated controller broadcasts don't spam /api/config.
  const sendAssistantConfig = createAssistantConfigSyncer(
    (payload) => httpJson("POST", `${sidecar.baseUrl()}/api/config`, payload, 3000)
  );
  async function syncAssistantConfig() {
    try {
      await sendAssistantConfig(getAssistantPrefsSnapshot());
    } catch {}
  }

  // Proactive reminder/briefing narration chime — same asset + player as
  // every other notification sound (theme "complete" sound via main's
  // playSound, which also honors mute/DND/cooldown gating).
  function maybePlayReminderChime() {
    try {
      if (getAssistantPrefsSnapshot().reminderChime === false) return;
      if (typeof ctx.playNotificationSound === "function") ctx.playNotificationSound("complete");
    } catch {}
  }

  // ── Adapter (LoRA) path resolution ────────────────────────────────────
  // Same shape as the model paths: <userData>/adapters/ in packaged
  // mode, <repo>/adapters/ in dev. The sidecar gateway scans this dir
  // for *.gguf files at boot and exposes them via /api/adapters; the
  // Settings tab lets the user pick which one is active.
  //
  // Bundled defaults live in <resources>/adapters/ (filled by
  // electron-builder extraResources). On first launch we copy any
  // *.gguf in there into the user dir so the file is editable by the
  // user (delete, rename) and visible in Finder via the same "open
  // adapter folder" shortcut.
  //
  // These helpers live here (before `new Sidecar(...)`) because the
  // seed + dir resolution must happen synchronously at boot, before
  // anything reads them. `const` has TDZ semantics so moving the
  // declarations earlier than their callers is mandatory; the model
  // helpers further down don't have this problem because nothing reads
  // them until IPC handlers fire.
  const ADAPTERS_SUBDIR = "adapters";
  function getDefaultAdapterDir() {
    if (app && app.isPackaged) {
      return path.join(getUserDataDir(), ADAPTERS_SUBDIR);
    }
    return path.resolve(appRoot, "..", ADAPTERS_SUBDIR);
  }
  function getBundledAdapterDir() {
    // process.resourcesPath only exists in packaged builds; dev builds
    // already point getDefaultAdapterDir at the repo so no seeding is
    // needed.
    try {
      if (app && app.isPackaged && process.resourcesPath) {
        return path.join(process.resourcesPath, ADAPTERS_SUBDIR);
      }
    } catch {}
    return null;
  }
  // Wrapper around the module-level pure function so we can unit-test
  // the copy walker without needing to mock Electron's `app` / `process`.
  function seedBundledAdapters() {
    const src = getBundledAdapterDir();
    if (!src) return;
    const dst = getDefaultAdapterDir();
    seedAdaptersFromBundle(src, dst, fs, log);
  }
  function getEffectiveAdapterDir() {
    if (process.env.MINICPM_ADAPTER_DIR) return process.env.MINICPM_ADAPTER_DIR;
    let raw = {};
    try { raw = readMinicpmPrefsRaw(); } catch {}
    if (typeof raw.adapter_dir === "string" && raw.adapter_dir.trim()) {
      return raw.adapter_dir.trim();
    }
    return getDefaultAdapterDir();
  }

  // ── Active adapter persistence ────────────────────────────────────────
  // We persist the user's choice of "currently active LoRA" so the next
  // sidecar spawn loads it directly via --lora (instead of preloading
  // every .gguf we find on disk just in case). Storage key is the
  // manifest entry's stable `id` (e.g. "preset:nekoqa" / "upload:...");
  // a path lookup at spawn time resolves it against the latest manifest,
  // so renames / moves don't break the link. `null` (or missing key)
  // means "start in pure Base mode — no LoRA loaded".
  function getActiveAdapterId() {
    let raw = {};
    try { raw = readMinicpmPrefsRaw(); } catch {}
    if (typeof raw.active_adapter_id === "string" && raw.active_adapter_id.trim()) {
      return raw.active_adapter_id.trim();
    }
    return null;
  }
  function setActiveAdapterId(id) {
    // null / "" clears the persisted choice → next launch boots Base.
    const next = (typeof id === "string" && id.trim()) ? id.trim() : null;
    mergeMinicpmPrefs({ active_adapter_id: next });
    return next;
  }
  function resolveActiveAdapterPath() {
    const id = getActiveAdapterId();
    if (!id) return null;
    const manifest = readAdapterManifest();
    const entry = (manifest.items || []).find((it) => it && it.id === id);
    if (!entry || !entry.path) return null;
    try {
      if (!fs.existsSync(entry.path)) return null;
    } catch { return null; }
    return entry.path;
  }

  // ── Adapter manifest (display names + aliases) ────────────────────────
  //
  // The gateway only knows about physical *.gguf files and a coarse
  // persona slug derived from filename hints. Everything user-facing —
  // friendly names like "猫娘 宝宝" and the alias list that powers chat
  // commands ("切到猫娘") — lives in this manifest, owned by the Electron
  // main process. Two consumers read it:
  //
  //   1. The Settings UI (via IPC) — for rendering chip labels and
  //      letting users rename / delete / upload entries.
  //   2. The sidecar (gateway) — we drop a copy as `.manifest.json` in
  //      the adapter dir so gateway can merge displayName/aliases into
  //      its `/api/adapters` response, which the chat bubble HTML
  //      reads directly (the chat web view has no preload bridge).
  //
  // Schema is documented in adapters/README.md.
  const ADAPTER_MANIFEST_FILE = "minicpm-adapters.json";
  // Mirror file the gateway reads on every /api/adapters call. Lives in
  // the adapter dir so a single watch / FS lookup is enough; the dot
  // prefix keeps it out of the *.gguf scan.
  const ADAPTER_MANIFEST_MIRROR = ".manifest.json";

  // Built-in presets that ship with the app. After the bundled .gguf
  // files have been copied into <userData>/adapters/ on first launch we
  // resolve `filenameHint` against the actual on-disk file and write a
  // manifest entry — so the user sees "猫娘 宝宝" the first time they
  // open Settings without any extra UI interaction.
  const DEFAULT_PRESET_ENTRIES = [
    {
      id: "preset:nekoqa",
      displayName: "Neko",
      aliases: ["neko", "catgirl", "baby"],
      persona: "neko",
      filenameHint: "lora_nekoqa",
    },
  ];

  function adapterManifestPath() {
    return path.join(getUserDataDir(), ADAPTER_MANIFEST_FILE);
  }
  function emptyManifest() {
    return { version: 1, items: [] };
  }
  function readAdapterManifest() {
    const p = adapterManifestPath();
    try {
      if (!fs.existsSync(p)) return emptyManifest();
      return parseManifestJson(fs.readFileSync(p, "utf-8"));
    } catch (err) {
      log(`[minicpm] adapter manifest read failed: ${err && err.message}`);
      return emptyManifest();
    }
  }
  function writeAdapterManifest(obj) {
    const p = adapterManifestPath();
    try {
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, JSON.stringify(obj || emptyManifest(), null, 2), "utf-8");
      // Mirror to the adapter dir for the gateway to read. Strip the
      // `id` field (gateway doesn't need internal identifiers) and
      // re-key by path so the gateway can resolve in O(1).
      try {
        const dir = getEffectiveAdapterDir();
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(
          path.join(dir, ADAPTER_MANIFEST_MIRROR),
          JSON.stringify(obj || emptyManifest(), null, 2),
          "utf-8",
        );
      } catch (mirrorErr) {
        log(`[minicpm] adapter manifest mirror failed: ${mirrorErr && mirrorErr.message}`);
      }
      return true;
    } catch (err) {
      log(`[minicpm] adapter manifest write failed: ${err && err.message}`);
      return false;
    }
  }
  function upsertAdapterEntry(entry) {
    if (!entry || !entry.id) return null;
    const manifest = readAdapterManifest();
    manifest.items = manifestUpsertItem(manifest.items, entry);
    writeAdapterManifest(manifest);
    return manifest.items.find((it) => it.id === entry.id) || null;
  }
  function removeAdapterEntry(id) {
    const manifest = readAdapterManifest();
    const before = manifest.items.length;
    manifest.items = manifestRemoveItem(manifest.items, id);
    if (manifest.items.length === before) return false;
    writeAdapterManifest(manifest);
    return true;
  }
  // Walk the adapter dir for a .gguf whose filename includes `hint`
  // (case-insensitive). Returns the absolute path, or null. Used by
  // seedDefaultManifest to bind built-in presets to whichever file
  // electron-builder actually shipped (filenames carry timestamps so
  // we can't hardcode them).
  function findAdapterByHint(dir, hint) {
    if (!hint) return null;
    const needle = String(hint).toLowerCase();
    function walk(cur) {
      let entries;
      try { entries = fs.readdirSync(cur, { withFileTypes: true }); }
      catch { return null; }
      for (const e of entries) {
        const p = path.join(cur, e.name);
        if (e.isDirectory()) {
          const hit = walk(p);
          if (hit) return hit;
          continue;
        }
        if (!e.isFile()) continue;
        const lower = e.name.toLowerCase();
        if (!lower.endsWith(".gguf")) continue;
        // Match against both filename and the immediate parent dir name
        // so a hint like "lora_nekoqa" hits the file even when the
        // .gguf itself is named generically (adapter_model.f16.gguf).
        const parent = path.basename(path.dirname(p)).toLowerCase();
        if (lower.includes(needle) || parent.includes(needle)) return p;
      }
      return null;
    }
    return walk(dir);
  }
  // First-run helper: if the user has no manifest yet, build one from
  // DEFAULT_PRESET_ENTRIES by resolving each preset's filenameHint
  // against the adapter dir. Skips presets whose backing .gguf isn't
  // there (e.g. the user deleted it before first launch).
  //
  // Idempotent: only writes when the manifest is missing or empty;
  // subsequent launches see the user's choices and don't touch them.
  function seedDefaultManifest() {
    const existing = readAdapterManifest();
    if (existing.items && existing.items.length > 0) return;
    const dir = getEffectiveAdapterDir();
    const items = [];
    for (const preset of DEFAULT_PRESET_ENTRIES) {
      const matched = findAdapterByHint(dir, preset.filenameHint);
      if (!matched) {
        log(`[minicpm] preset ${preset.id} has no matching .gguf in ${dir}, skipping seed`);
        continue;
      }
      items.push({
        id: preset.id,
        path: matched,
        displayName: preset.displayName,
        aliases: Array.isArray(preset.aliases) ? [...preset.aliases] : [],
        persona: preset.persona || "default",
        source: "bundled",
        createdAt: new Date().toISOString(),
      });
    }
    writeAdapterManifest({ version: 1, items });
  }

  // Repair pass: bundled-preset entries whose `path` no longer exists
  // (because the user moved their dev checkout, reinstalled the app
  // under a different userData, etc.) get re-bound to whatever .gguf
  // their `filenameHint` resolves to in the current adapter dir. User-
  // upload entries are NEVER auto-repaired — they're surfaced as
  // `missing: true` in the UI so the user can decide what to do.
  function repairBundledManifestPaths() {
    const manifest = readAdapterManifest();
    if (!manifest.items || manifest.items.length === 0) return;
    const dir = path.resolve(getEffectiveAdapterDir());
    let dirty = false;
    for (const entry of manifest.items) {
      if (!entry || entry.source !== "bundled") continue;
      let needsRepair = true;
      try {
        if (entry.path && fs.existsSync(entry.path)) {
          // Existence alone isn't enough: when the user switches between
          // dev (`<repo>/adapters/`) and packaged (`<userData>/adapters/`),
          // the previous run's path may still resolve on disk while the
          // gateway scans a different dir. Only treat the entry as
          // healthy when its path lives under the *current* effective
          // adapter dir — otherwise the IPC merge layer can't match it
          // up with what gateway returns and the chip falls into the
          // missing-file branch.
          const resolvedEntry = path.resolve(entry.path);
          if (resolvedEntry === dir || resolvedEntry.startsWith(dir + path.sep)) {
            needsRepair = false;
          }
        }
      } catch {}
      if (!needsRepair) continue;
      const preset = DEFAULT_PRESET_ENTRIES.find((p) => p.id === entry.id);
      if (!preset || !preset.filenameHint) continue;
      const found = findAdapterByHint(dir, preset.filenameHint);
      if (!found) {
        log(`[minicpm] bundled preset ${entry.id} path '${entry.path}' missing in ${dir}`);
        continue;
      }
      log(`[minicpm] repaired ${entry.id} path: ${entry.path} -> ${found}`);
      entry.path = found;
      dirty = true;
    }
    if (dirty) writeAdapterManifest(manifest);
  }

  // Recursively list *.gguf under `rootDir` with mtime, skipping the
  // staging / backup dirs the gateway also ignores (server.py). Feeds
  // the pure planBundledReconcile().
  function listAdapterGgufs(rootDir) {
    const out = [];
    function walk(cur) {
      let entries;
      try { entries = fs.readdirSync(cur, { withFileTypes: true }); }
      catch { return; }
      for (const e of entries) {
        const p = path.join(cur, e.name);
        if (e.isDirectory()) {
          if (e.name.endsWith(".bak") || e.name.endsWith(".update-staging")) continue;
          walk(p);
          continue;
        }
        if (!e.isFile() || !e.name.toLowerCase().endsWith(".gguf")) continue;
        let mtimeMs = 0;
        try { mtimeMs = fs.statSync(p).mtimeMs; } catch {}
        out.push({ path: p, name: e.name, mtimeMs });
      }
    }
    walk(rootDir);
    return out;
  }

  // Reconcile bundled presets after seed + repair: when a newer copy of a
  // preset's adapter has been seeded alongside an older one, re-point the
  // manifest at the newest and delete the stale copies, so Settings stops
  // showing a duplicate persona chip (one stuck on the raw .gguf name).
  // Only touches files matching a preset hint that no user-upload entry
  // claims; the kept copy and the adapter root are never delete targets.
  function reconcileBundledDuplicates() {
    const dir = path.resolve(getEffectiveAdapterDir());
    const scanned = listAdapterGgufs(dir);
    const plan = planBundledReconcile({
      scanned,
      presets: DEFAULT_PRESET_ENTRIES,
      manifestItems: readAdapterManifest().items,
    });
    for (const r of plan.repoint) {
      upsertAdapterEntry({ id: r.id, path: r.path });
      log(`[minicpm] reconcile re-pointed ${r.id} -> ${r.path}`);
    }
    for (const filePath of plan.superseded) {
      const { kind, target } = safeDeleteTargetFor(filePath, dir);
      if (kind === "skip" || !target) {
        log(`[minicpm] reconcile skipped unsafe delete target: ${filePath}`);
        continue;
      }
      try {
        fs.rmSync(target, { recursive: true, force: true });
        log(`[minicpm] reconcile removed superseded ${kind}: ${target}`);
      } catch (err) {
        log(`[minicpm] reconcile delete failed for ${target}: ${err && err.message}`);
      }
    }
  }

  // Copy bundled adapters (from <resources>/adapters/) into the
  // writable user dir on first run. Cheap idempotent walk; skips
  // anything the user already has. Runs once before the sidecar
  // spawns so /api/adapters returns the seeded files immediately.
  try { seedBundledAdapters(); } catch (err) {
    log(`[minicpm] seedBundledAdapters threw: ${err && err.message}`);
  }
  const adapterDir = getEffectiveAdapterDir();
  try { fs.mkdirSync(adapterDir, { recursive: true }); } catch {}
  // Manifest seed runs AFTER the .gguf copy so filenameHint lookups
  // can resolve against actual disk files. Also writes the mirror for
  // the gateway to read on its first /api/adapters call.
  try { seedDefaultManifest(); } catch (err) {
    log(`[minicpm] seedDefaultManifest threw: ${err && err.message}`);
  }
  // After seeding, repair any bundled preset whose recorded path went
  // stale (typical when a dev manifest got carried into a packaged
  // install, or vice versa).
  try { repairBundledManifestPaths(); } catch (err) {
    log(`[minicpm] repairBundledManifestPaths threw: ${err && err.message}`);
  }
  // Drop duplicate persona chips: re-point each bundled preset to its
  // newest on-disk copy and delete superseded ones (e.g. an old nekoqa
  // adapter left behind after a newer build was seeded in).
  try { reconcileBundledDuplicates(); } catch (err) {
    log(`[minicpm] reconcileBundledDuplicates threw: ${err && err.message}`);
  }
  // Always ensure the mirror exists, even when the manifest is non-empty
  // (user already has a manifest from a previous launch, but the mirror
  // file may be missing if they upgraded across the change).
  try {
    const dir = getEffectiveAdapterDir();
    const mirror = path.join(dir, ADAPTER_MANIFEST_MIRROR);
    if (!fs.existsSync(mirror)) {
      writeAdapterManifest(readAdapterManifest());
    }
  } catch {}

  // Full gateway sidecar (FastAPI on :18765) — it spawns and owns its
  // own llama-server, and provides /api/chat, personas (LoRA), thinking
  // filter, model discovery and tool hooks. The earlier direct
  // LlamaServerManager wiring bypassed all of that.
  const sidecar = new Sidecar({
    sidecarDir,
    sidecarBin,
    appRoot,
    port,
    host: process.env.MINICPM_HOST || DEFAULT_HOST,
    log,
    logFile: sidecarLogPath,
    adapterDir: getEffectiveAdapterDir(),
    modelPresent: (dir) => isModelPresent(dir),
  });
  // Refresh `sidecar.activeAdapterPath` from prefs every time we're about
  // to spawn. Lets the user pick a persona, restart the sidecar from
  // Settings, and have the new choice take effect — without needing to
  // wire Sidecar.start() into closure-only helpers.
  function refreshActiveAdapterPath() {
    try {
      sidecar.activeAdapterPath = resolveActiveAdapterPath();
    } catch (err) {
      sidecar.activeAdapterPath = null;
      log(`[minicpm] resolveActiveAdapterPath failed: ${err && err.message}`);
    }
    return sidecar.activeAdapterPath;
  }
  // First evaluation: pick up whatever the user had selected last
  // session. `null` means "boot Base, no --lora".
  refreshActiveAdapterPath();

  let bubble = null;
  // Resolves when the bubble's renderer page has finished loading and its
  // IPC listeners (onOpen etc.) are registered. Without awaiting this,
  // cmd-open sent right after createBubble() is dropped and the bubble
  // never paints (transparent window, phase stays "hidden").
  let bubbleReady = null;
  let activeSide = "right";
  // Updated from /api/health after the sidecar comes online — drives the
  // narrator's voice (default vs neko etc.).
  let activePersona = "default";
  // Tracked "is the bubble currently shown to the user" flag. We can't rely
  // on bubble.isVisible() with macOS panel windows because showInactive() +
  // panel quirks make it return true even after a hide().
  let bubbleShown = false;
  // When set, bubble resizes pin their bottom edge to this Y so the
  // textarea stays put while the bubble grows upward. Cleared on
  // open/transition. Renderer sets this via the "resize" IPC.
  let chatAnchorBottomY = null;
  // Cached "is there a new model on the remote?" status. Refreshed on
  // launch, after every apply, and whenever the user manually checks.
  let updateStatus = null; // { available, local_revision, remote_revision, ... }

  // ── Sidecar crash auto-restart ────────────────────────────────────────
  // The gateway is normally stable, but a segfault, OOM kill, or an
  // external taskkill on :18765 used to leave the pet mute until app
  // relaunch. On unplanned exit we retry with escalating backoff
  // (2s → 5s → 10s); each state change is broadcast to the bubble so
  // the renderer can show "restarting…" / "offline" instead of a hung
  // request. A successful attempt resets the attempt counter.
  const SIDECAR_RESTART_DELAYS_MS = [2000, 5000, 10000];
  let sidecarRestartAttempt = 0;
  let sidecarRestartTimer = null;
  let sidecarRestarting = false;
  let sidecarShuttingDown = false;

  function broadcastSidecarState(state, extra = {}) {
    try {
      if (bubble && !bubble.isDestroyed()) {
        bubble.webContents.send("minicpm:sidecar-state", { state, ...extra });
      }
    } catch {}
  }

  function scheduleSidecarRestart(reason) {
    if (sidecarShuttingDown || sidecarRestarting) return;
    if (sidecarRestartAttempt >= SIDECAR_RESTART_DELAYS_MS.length) {
      log(`[minicpm] sidecar restart gave up after ${sidecarRestartAttempt} attempts (${reason})`);
      broadcastSidecarState("down", { reason });
      return;
    }
    const delay = SIDECAR_RESTART_DELAYS_MS[sidecarRestartAttempt];
    sidecarRestartAttempt += 1;
    log(`[minicpm] scheduling sidecar restart #${sidecarRestartAttempt} in ${delay}ms (${reason})`);
    broadcastSidecarState("restarting", { attempt: sidecarRestartAttempt });
    sidecarRestartTimer = setTimeout(() => { void attemptSidecarRestart(reason); }, delay);
    if (typeof sidecarRestartTimer.unref === "function") sidecarRestartTimer.unref();
  }

  async function attemptSidecarRestart(reason) {
    sidecarRestarting = true;
    try {
      // Re-read the persisted LoRA choice so a respawn boots into
      // whatever persona the user had active (same as explicit restarts).
      refreshActiveAdapterPath();
      await sidecar.ensureRunning(getEffectiveModelDir());
      sidecarRestartAttempt = 0;
      log(`[minicpm] sidecar restarted OK (previous exit: ${reason})`);
      broadcastSidecarState("back");
    } catch (err) {
      log(`[minicpm] sidecar restart attempt #${sidecarRestartAttempt} failed: ${err && err.message}`);
      scheduleSidecarRestart(reason);
    } finally {
      sidecarRestarting = false;
    }
  }

  sidecar.onUnexpectedExit = (code, signal) => {
    scheduleSidecarRestart(`exit code=${code} signal=${signal}`);
  };

  // ── Chat generation parameters ────────────────────────────────────────
  // Persisted to <userData>/minicpm-prefs.json so they survive restart.
  // Values are validated/clamped on every set; the chat bubble fetches
  // them on each submit, the Settings tab reads/writes via IPC.
  //
  // Several unrelated settings share this file (chat params, `model_dir`,
  // `narration_enabled`, ...). Every writer MUST go through
  // `mergeMinicpmPrefs()` — a naive `JSON.stringify(chatParams)` would erase
  // `model_dir` the next time the user toggled "thinking", etc.
  const DEFAULT_CHAT_PARAMS = {
    max_new_tokens: 768,
    temperature: 0.6,
    top_p: 0.95,
    top_k: 0,                  // 0 = disabled
    repetition_penalty: 1.05,
    thinking: false,           // default off (LoRA usually wasn't trained on <think>)
  };
  const CHAT_PARAM_KEYS = Object.keys(DEFAULT_CHAT_PARAMS);

  function readMinicpmPrefsRaw() {
    try {
      if (!fs.existsSync(PARAMS_PATH)) return {};
      const parsed = JSON.parse(fs.readFileSync(PARAMS_PATH, "utf-8"));
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (err) {
      log(`[minicpm] prefs read failed: ${err && err.message}`);
      return {};
    }
  }

  function writeMinicpmPrefsRaw(raw) {
    try {
      fs.writeFileSync(PARAMS_PATH, JSON.stringify(raw, null, 2), "utf-8");
      return true;
    } catch (err) {
      log(`[minicpm] prefs save failed: ${err && err.message}`);
      return false;
    }
  }

  // Read-modify-write merge so each setting only touches its own keys.
  // Passing `undefined` for a key removes it from the persisted file.
  function mergeMinicpmPrefs(partial) {
    if (!partial || typeof partial !== "object") return false;
    const current = readMinicpmPrefsRaw();
    for (const key of Object.keys(partial)) {
      const next = partial[key];
      if (next === undefined) delete current[key];
      else current[key] = next;
    }
    return writeMinicpmPrefsRaw(current);
  }

  // Bootstrap chatParams restricted to known keys so unrelated sibling
  // fields (model_dir / narration_enabled / ...) can't leak into the in-
  // memory chatParams and accidentally get echoed back on the next save.
  let chatParams = { ...DEFAULT_CHAT_PARAMS };
  {
    const raw = readMinicpmPrefsRaw();
    for (const key of CHAT_PARAM_KEYS) {
      if (Object.prototype.hasOwnProperty.call(raw, key)) chatParams[key] = raw[key];
    }
  }
  function clampChatParams(input) {
    const out = { ...chatParams };
    if (!input || typeof input !== "object") return out;
    if (Number.isFinite(input.max_new_tokens))
      out.max_new_tokens = Math.max(16, Math.min(4096, Math.floor(input.max_new_tokens)));
    if (Number.isFinite(input.temperature))
      out.temperature = Math.max(0, Math.min(2, Number(input.temperature)));
    if (Number.isFinite(input.top_p))
      out.top_p = Math.max(0.05, Math.min(1, Number(input.top_p)));
    if (Number.isFinite(input.top_k))
      out.top_k = Math.max(0, Math.min(200, Math.floor(input.top_k)));
    if (Number.isFinite(input.repetition_penalty))
      out.repetition_penalty = Math.max(1, Math.min(2, Number(input.repetition_penalty)));
    if (typeof input.thinking === "boolean") out.thinking = input.thinking;
    return out;
  }
  // Re-clamp bootstrap so a corrupt persisted value (e.g. max_new_tokens
  // outside range) doesn't ride along into runtime.
  chatParams = clampChatParams(chatParams);
  function setChatParams(input) {
    chatParams = clampChatParams(input);
    mergeMinicpmPrefs(chatParams);
    return chatParams;
  }
  function getChatParams() { return { ...chatParams }; }

  // ── Model path resolution ─────────────────────────────────────────────
  // Production: <userData>/models/<model>.gguf (downloaded by Onboarding).
  // Dev: <repo>/models/<model>.gguf (developer convenience).
  // Users can override via Settings → MiniCPM → 本地模型路径 (writes
  // minicpm-prefs.json model_dir field), or MINICPM_MODEL_DIR env at launch.
  //
  // Legacy v0.7.x onboarding wrote a HuggingFace directory path here. We
  // accept either form: if the configured path is a directory, we scan
  // it for a *.gguf inside; if it's a file, we use it as-is.
  const MODELS_SUBDIR = "models";
  function getUserDataDir() {
    try { return app.getPath("userData"); } catch { return os.tmpdir(); }
  }
  function getDefaultModelDir() {
    if (app && app.isPackaged) {
      return path.join(getUserDataDir(), MODELS_SUBDIR);
    }
    return path.resolve(appRoot, "..", MODELS_SUBDIR);
  }
  function _firstGgufIn(dir) {
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      // Direct hit first
      const here = entries
        .filter((e) => e.isFile() && e.name.toLowerCase().endsWith(".gguf"))
        .map((e) => path.join(dir, e.name));
      if (here.length) return here[0];
      // One level deep (Onboarding may have nested by repo name)
      for (const e of entries) {
        if (!e.isDirectory()) continue;
        const sub = path.join(dir, e.name);
        try {
          const inner = fs.readdirSync(sub)
            .filter((n) => n.toLowerCase().endsWith(".gguf"));
          if (inner.length) return path.join(sub, inner[0]);
        } catch {}
      }
    } catch {}
    return null;
  }
  function getEffectiveModelDir() {
    if (process.env.MINICPM_MODEL_DIR) return process.env.MINICPM_MODEL_DIR;
    const raw = readMinicpmPrefsRaw();
    if (typeof raw.model_dir === "string" && raw.model_dir.trim()) {
      return raw.model_dir.trim();
    }
    return getDefaultModelDir();
  }
  function setEffectiveModelDir(dir) {
    const next = (typeof dir === "string" && dir.trim()) ? dir.trim() : undefined;
    mergeMinicpmPrefs({ model_dir: next });
    return getEffectiveModelDir();
  }
  function isModelPresent(dir) {
    const target = dir || getEffectiveModelDir();
    try {
      const st = fs.statSync(target);
      if (st.isFile()) return target.toLowerCase().endsWith(".gguf");
      if (st.isDirectory()) return _firstGgufIn(target) !== null;
    } catch {}
    return false;
  }
  function resolveCurrentGgufPath(healthJson) {
    const candidates = [];
    if (healthJson && healthJson.model_dir) candidates.push(healthJson.model_dir);
    candidates.push(getEffectiveModelDir());
    for (const candidate of candidates) {
      if (!candidate) continue;
      try {
        const st = fs.statSync(candidate);
        if (st.isFile() && candidate.toLowerCase().endsWith(".gguf")) return candidate;
        if (st.isDirectory()) {
          const gguf = _firstGgufIn(candidate);
          if (gguf) return gguf;
        }
      } catch {}
    }
    return null;
  }

  // ── Process tree RSS (Settings → 资源占用) ───────────────────────────
  async function listAllProcesses() {
    if (isWin) {
      const ps =
        "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; " +
        "Get-CimInstance Win32_Process | " +
        "Select-Object ProcessId,ParentProcessId,WorkingSetSize,Name,CommandLine | " +
        "ConvertTo-Json -Compress";
      const { stdout } = await execFileAsync(
        "powershell.exe",
        ["-NoProfile", "-Command", ps],
        { maxBuffer: 12 * 1024 * 1024, windowsHide: true },
      );
      const parsed = JSON.parse(stdout || "[]");
      const arr = Array.isArray(parsed) ? parsed : (parsed ? [parsed] : []);
      return arr.map((p) => ({
        pid: Number(p.ProcessId),
        ppid: Number(p.ParentProcessId),
        rss: Math.round(Number(p.WorkingSetSize || 0) / 1024),
        cpu: 0,
        cmd: String(p.CommandLine || p.Name || ""),
      })).filter((p) => Number.isFinite(p.pid) && p.pid > 0);
    }
    const { stdout } = await execFileAsync(
      "ps",
      ["-axo", "pid=,ppid=,rss=,pcpu=,command="],
      { maxBuffer: 12 * 1024 * 1024 },
    );
    return stdout.trim().split("\n").map((line) => {
      const trimmed = line.trim();
      const m = trimmed.match(/^(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(.*)$/);
      if (!m) return null;
      return {
        pid: Number(m[1]),
        ppid: Number(m[2]),
        rss: Number(m[3]),
        cpu: parseFloat(m[4]) || 0,
        cmd: m[5] || "",
      };
    }).filter(Boolean);
  }
  function collectProcessTree(rootPid, allProcs) {
    const byPpid = new Map();
    for (const proc of allProcs) {
      if (!byPpid.has(proc.ppid)) byPpid.set(proc.ppid, []);
      byPpid.get(proc.ppid).push(proc);
    }
    const tree = [];
    const queue = [rootPid];
    const seen = new Set();
    while (queue.length) {
      const pid = queue.shift();
      if (seen.has(pid)) continue;
      seen.add(pid);
      const proc = allProcs.find((p) => p.pid === pid);
      if (proc) tree.push(proc);
      for (const child of byPpid.get(pid) || []) queue.push(child.pid);
    }
    return tree;
  }

  // ── Bubble position (side preference + drag offset) ───────────────────
  // Persisted alongside chat params in the same JSON file. The Settings
  // panel can switch the user into "edit mode" — the bubble becomes
  // window-draggable and shows sample text — and on save we capture the
  // (dx, dy) offset relative to the default placement for the chosen side.
  //
  // Schema:
  //   side: "left" | "right" | "auto"
  //   dx:   signed pixels, positive = further from the pet
  //   dy:   signed pixels, positive = downward from pet center
  //
  const BUBBLE_POS_PATH = (() => {
    try { return path.join(app.getPath("userData"), "minicpm-bubble-pos.json"); }
    catch { return path.join(os.tmpdir(), "minicpm-bubble-pos.json"); }
  })();
  // Default tuned by hand-positioning next to the actual pet sprite —
  // sits a touch closer to the body and slightly below the head so the
  // tail points at the cat's mouth instead of forehead.
  const DEFAULT_BUBBLE_POS = { side: "left", dx: -45, dy: 45 };
  let bubblePos = { ...DEFAULT_BUBBLE_POS };
  try {
    if (fs.existsSync(BUBBLE_POS_PATH)) {
      const raw = JSON.parse(fs.readFileSync(BUBBLE_POS_PATH, "utf-8"));
      if (raw && typeof raw === "object") bubblePos = { ...DEFAULT_BUBBLE_POS, ...raw };
    }
  } catch (err) { log(`[minicpm] bubble-pos load failed: ${err && err.message}`); }
  function clampBubblePos(input) {
    const out = { ...bubblePos };
    if (!input || typeof input !== "object") return out;
    if (input.side === "left" || input.side === "right" || input.side === "auto") out.side = input.side;
    if (Number.isFinite(input.dx)) out.dx = Math.max(-2000, Math.min(2000, Math.floor(input.dx)));
    if (Number.isFinite(input.dy)) out.dy = Math.max(-2000, Math.min(2000, Math.floor(input.dy)));
    return out;
  }
  function setBubblePos(input) {
    bubblePos = clampBubblePos(input);
    try { fs.writeFileSync(BUBBLE_POS_PATH, JSON.stringify(bubblePos, null, 2), "utf-8"); }
    catch (err) { log(`[minicpm] bubble-pos save failed: ${err && err.message}`); }
    return bubblePos;
  }
  function getBubblePos() { return { ...bubblePos }; }
  // True while the Settings panel has the bubble in "drag-to-position"
  // mode. Position writes (and the auto-hide / dwell logic) are paused
  // while this is true.
  let bubbleEditing = false;

  // ── Narration (model reacts to coding-agent events) ──────────────────────
  // Persisted under `narration_enabled` in minicpm-prefs.json so the user's
  // choice survives restart. Default true keeps the previous dev behaviour
  // when the key is missing (first launch after upgrade, fresh install).
  let narrationEnabled = (() => {
    const raw = readMinicpmPrefsRaw();
    return typeof raw.narration_enabled === "boolean" ? raw.narration_enabled : true;
  })();
  const NARRATE_THROTTLE_MS = 10_000;     // gap between any two narrations
  const SESSION_DEDUP_MS = 5_000;         // ignore repeats for the same session
  const QUEUED_EVENT_MAX_AGE_MS = 60_000; // drop stale queued events after chat ends
  // Events worth narrating. Anything else is dropped.
  const NARRATE_EVENTS = new Set(["Stop", "StopFailure", "Notification"]);
  // Skip when the event came from us (the chat sidecar pushes states too).
  const NARRATE_IGNORE_SESSION_PREFIX = "minicpm-";

  let lastNarrateAt = 0;
  let lastSessionAt = new Map(); // session_id -> timestamp
  // FIFO queue of events to narrate sequentially. Multiple windows
  // (different sessions) finishing close together each get their turn
  // instead of being deduplicated away. Max length keeps us from
  // chaining narrations forever if user steps away.
  const QUEUE_MAX = 5;
  let queuedEvents = [];        // [{ data, queuedAt }, ...]
  let narrating = false;

  function getPetBoundsSafe() {
    // Prefer the hit-rect (visible sprite) over the pet window — the
    // window has large transparent margins, anchoring to it makes the
    // bubble float far from the actual character.
    try {
      const hit = ctx.getPetHitRect && ctx.getPetHitRect();
      if (hit && Number.isFinite(hit.width) && hit.width > 0) {
        return { x: Math.round(hit.x), y: Math.round(hit.y), width: Math.round(hit.width), height: Math.round(hit.height) };
      }
    } catch {}
    try { return ctx.getPetWindowBounds && ctx.getPetWindowBounds(); } catch { return null; }
  }

  function getWorkAreaForPet(pb) {
    if (typeof ctx.getNearestWorkArea === "function" && pb) {
      try { return ctx.getNearestWorkArea(pb.x + pb.width / 2, pb.y + pb.height / 2); } catch {}
    }
    return screen.getPrimaryDisplay().workArea;
  }

  function chooseAndApplyBounds(width, height, { keepSide = false } = {}) {
    if (!bubble || bubble.isDestroyed()) return;
    const pb = getPetBoundsSafe();
    const wa = pb ? getWorkAreaForPet(pb) : screen.getPrimaryDisplay().workArea;
    if (pb) {
      // When the pet has moved (drag end / repos call) we re-pick the
      // best side so the bubble doesn't end up clamped over the pet's
      // sprite. `keepSide` is used during the same logical "show" so
      // size changes (e.g. ask → speak) don't flip sides mid-conversation.
      if (!keepSide || !activeSide) activeSide = pickSide(pb, wa, width, height, bubblePos.side);
      const opts = chatAnchorBottomY !== null
        ? { verticalAnchor: "bottom", anchorBottomY: chatAnchorBottomY }
        : {};
      opts.offsetDx = bubblePos.dx;
      opts.offsetDy = bubblePos.dy;
      const bounds = computeBubbleBoundsForSide(activeSide, pb, wa, width, height, opts);
      bubble.setBounds(bounds);
    } else {
      bubble.setBounds({
        x: Math.round((wa.width - width) / 2),
        y: Math.round((wa.height - height) / 2),
        width, height,
      });
    }
  }

  function reposition() {
    if (!bubble || bubble.isDestroyed() || !bubble.isVisible()) return;
    const { width, height } = bubble.getBounds();
    // During pet drag we keep recomputing on every move tick; let the
    // bubble re-pick side as the pet crosses regions so it never overlaps.
    chooseAndApplyBounds(width, height, { keepSide: false });
  }

  // Pet-drag awareness: hide the bubble while user is dragging the pet
  // (continuous reposition during drag is jittery and visually noisy);
  // restore it cleanly after the drop with a fresh side pick.
  let petDragging = false;
  let bubbleHiddenForDrag = false;
  function setPetDragging(v) {
    const wasDragging = petDragging;
    petDragging = !!v;
    if (!bubble || bubble.isDestroyed()) return;
    if (petDragging && !wasDragging && bubble.isVisible()) {
      // Drag started → fade away, remember to restore on drop.
      bubbleHiddenForDrag = true;
      try { bubble.hide(); } catch {}
    } else if (!petDragging && wasDragging && bubbleHiddenForDrag) {
      // Drag ended → re-show on the now-best side.
      bubbleHiddenForDrag = false;
      const { width, height } = bubble.getBounds();
      chooseAndApplyBounds(width, height, { keepSide: false });
      try { bubble.showInactive(); } catch {}
    }
  }

  function createBubble() {
    const pb = getPetBoundsSafe() || { x: 200, y: 200, width: 280, height: 280 };
    const wa = getWorkAreaForPet(pb);
    activeSide = pickSide(pb, wa, ASK_WIDTH, ASK_HEIGHT, bubblePos.side);
    const initial = computeBubbleBoundsForSide(activeSide, pb, wa, ASK_WIDTH, ASK_HEIGHT, {
      offsetDx: bubblePos.dx, offsetDy: bubblePos.dy,
    });

    bubble = new BrowserWindow({
      ...initial,
      minWidth: MIN_WIDTH,
      minHeight: MIN_HEIGHT,
      show: false,
      frame: false,
      transparent: true,
      hasShadow: false,
      resizable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      focusable: true,
      // Alt-Tab / taskbar previews should carry the product mark, not the
      // stock Electron icon.
      icon: path.join(__dirname, "..", "assets", "icons", "256x256.png"),
      ...(isLinux ? { type: LINUX_WINDOW_TYPE } : {}),
      ...(isMac ? { type: "panel" } : {}),
      webPreferences: {
        preload: path.join(__dirname, "preload-minicpm-chat.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });

    if (isWin) bubble.setAlwaysOnTop(true, WIN_TOPMOST_LEVEL);
    if (isMac) {
      try { bubble.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }); } catch {}
    }
    bubble.setMenuBarVisibility(false);
    // Bypass any cached HTML so code changes always take effect.
    bubble.webContents.session.clearCache();
    bubbleReady = new Promise((resolve) => {
      const wc = bubble.webContents;
      if (!wc.isLoading()) resolve();
      else wc.once("did-finish-load", resolve);
    });
    bubble.loadFile(path.join(__dirname, "minicpm-chat.html"));

    bubble.webContents.on("before-input-event", (event, input) => {
      if (input.type === "keyDown" && input.key === "Escape") {
        // Renderer treats Esc itself; this is just a safety net.
        try { bubble.hide(); } catch {}
        event.preventDefault();
      }
    });
    bubble.on("closed", () => { bubble = null; });

    // Diagnostics: surface renderer console + load failures in the main log.
    bubble.webContents.on("console-message", (_e, level, message) => {
      log(`[bubble-console:${level}] ${message}`);
    });
    bubble.webContents.on("did-fail-load", (_e, code, desc, url) => {
      log(`[minicpm-chat] bubble did-fail-load: ${code} ${desc} (${url})`);
    });

    return bubble;
  }

  function ensureBubble() {
    if (!bubble || bubble.isDestroyed()) createBubble();
    return bubble;
  }

  async function waitForBubbleReady() {
    if (bubbleReady) await bubbleReady;
  }

  async function open() {
    ensureBubble();
    // Re-pick the side based on the current pet position each time we open.
    const pb = getPetBoundsSafe();
    const wa = pb ? getWorkAreaForPet(pb) : screen.getPrimaryDisplay().workArea;
    activeSide = pb ? pickSide(pb, wa, ASK_WIDTH, ASK_HEIGHT, bubblePos.side) : (bubblePos.side === "left" ? "left" : "right");
    if (pb) {
      bubble.setBounds(computeBubbleBoundsForSide(activeSide, pb, wa, ASK_WIDTH, ASK_HEIGHT, {
        offsetDx: bubblePos.dx, offsetDy: bubblePos.dy,
      }));
    }
    if (!bubble.isVisible()) bubble.show();
    bubble.focus();
    bubbleShown = true;
    // Only send cmd-open once the renderer has registered its listeners;
    // otherwise the first open after createBubble() is silently dropped.
    await waitForBubbleReady();
    bubble.webContents.send("minicpm:cmd-open", { side: activeSide });
    // Fire a 1-token warmup so the model weights are paged back into RAM
    // by the time the user finishes typing. Throttled — repeated opens
    // within 30s don't re-warm (model is still hot).
    void maybeWarmup();
  }

  // ── Warmup ping ────────────────────────────────────────────────────────
  // macOS pages out the model's memory after the sidecar has been idle
  // for a few minutes; the first request then takes 1-3s instead of
  // 0.1s. We fire `/api/warmup` on every bubble open. The endpoint runs
  // a 1-token greedy forward (~50-200ms hot, ~1-2s cold) which faults
  // the weights back in so the user's actual chat call is fast.
  let lastWarmupAt = 0;
  const WARMUP_GAP_MS = 30_000;  // 30s — covers fast re-opens / multi-turn chat
  async function maybeWarmup() {
    const now = Date.now();
    if (now - lastWarmupAt < WARMUP_GAP_MS) return;
    lastWarmupAt = now;
    try {
      // 5s timeout — plenty for cold start, won't pile up if sidecar is slow.
      await httpJson("POST", `${sidecar.baseUrl()}/api/warmup`, {}, 5000);
    } catch (err) {
      log(`[minicpm] warmup ping failed: ${err && err.message || err}`);
    }
  }

  function toggle() {
    if (bubble && !bubble.isDestroyed() && bubble.isVisible()) {
      bubble.webContents.send("minicpm:cmd-dismiss");
      return;
    }
    open();
  }

  function dismiss() {
    if (bubble && !bubble.isDestroyed()) bubble.webContents.send("minicpm:cmd-dismiss");
  }

  function toggleThinking() {
    // The renderer owns the flag; we just nudge it to flip and toast.
    // If the bubble doesn't exist yet, ensure it does so the listener attaches.
    ensureBubble();
    void waitForBubbleReady().then(() => {
      if (bubble && !bubble.isDestroyed()) bubble.webContents.send("minicpm:cmd-toggle-thinking");
    });
  }

  function shutdown() {
    sidecarShuttingDown = true;
    if (sidecarRestartTimer) { clearTimeout(sidecarRestartTimer); sidecarRestartTimer = null; }
    sidecar.stop();
    if (bubble && !bubble.isDestroyed()) bubble.destroy();
    bubble = null;
  }

  // ── Narration logic ─────────────────────────────────────────────────────
  // Score how "rich" an event is for narration. Higher is better.
  // We use this to pick the best of multiple events that fire for the
  // same logical conversation (Cursor + Claude Code hooks both fire on
  // Cursor's stop, but only the cursor-agent variant has session_title
  // and last_summary populated by the hook's transcript parser).
  function eventRichness(data) {
    let s = 0;
    if (typeof data.session_title === "string" && data.session_title.trim()) s += 10;
    if (typeof data.last_summary === "string" && data.last_summary.trim()) s += 10;
    if (typeof data.assistant_last_output === "string" && data.assistant_last_output.trim()) s += 10;
    if (data.agent_id === "cursor-agent") s += 1;  // tie-breaker
    return s;
  }

  // Per-session merge buffer: when an event arrives, hold it for
  // EVENT_MERGE_MS waiting for a sibling event for the same session
  // (e.g., Cursor's claude-code companion). Whichever has the richer
  // context wins. Without this, the claude-code event arrives ~ms
  // earlier and gets dispatched with empty title/summary, giving us
  // generic "主人刚写完一轮代码" prompts.
  const EVENT_MERGE_MS = 700;
  const eventBuffers = new Map();  // sessionId → { data, score, timer }

  function onStateEvent(data) {
    if (!narrationEnabled) return;
    if (bubbleEditing) return;  // Don't intrude while the user is positioning the bubble.
    if (!data || typeof data !== "object") return;
    const event = String(data.event || "");
    const sessionId = String(data.session_id || "");
    if (!NARRATE_EVENTS.has(event)) return;
    if (sessionId.startsWith(NARRATE_IGNORE_SESSION_PREFIX)) return;

    // Proactive sidecar lines (reminders, daily briefings) carry their
    // final text in session_title — show them verbatim instead of
    // running the narration model.
    if (sessionId.startsWith("deskpet-proactive")) {
      const line = typeof data.session_title === "string" ? data.session_title.trim() : "";
      if (line) {
        // Native Windows toast first (more visible); bubble narration is
        // the fallback when the main-process hook is unavailable.
        if (typeof ctx.showSystemNotification === "function") {
          maybePlayReminderChime();
          ctx.showSystemNotification("Deskpet Assistant", line);
        } else {
          showNarrationLine(line, event, String(data.agent_id || ""));
        }
      }
      return;
    }

    const now = Date.now();
    // Per-session "already dispatched" gate (5s after final commit).
    const last = lastSessionAt.get(sessionId);
    if (last && (now - last) < SESSION_DEDUP_MS) {
      log(`[narrator] drop: session ${sessionId.slice(0,8)} dedup ${now - last}ms`);
      return;
    }

    const score = eventRichness(data);
    const buf = eventBuffers.get(sessionId);
    if (buf) {
      // Already buffered — keep whichever has more context.
      if (score > buf.score) {
        log(`[narrator] merge: session ${sessionId.slice(0,8)} replace agent=${buf.data.agent_id}→${data.agent_id} (score ${buf.score}→${score})`);
        buf.data = data;
        buf.score = score;
      } else {
        log(`[narrator] merge: session ${sessionId.slice(0,8)} keep agent=${buf.data.agent_id} (score ${buf.score} ≥ ${score})`);
      }
      return;
    }

    // First event for this session — start the merge window.
    log(`[narrator] buffer: event=${event} session=${sessionId.slice(0,8)} agent=${data.agent_id} score=${score} (waiting ${EVENT_MERGE_MS}ms for siblings)`);
    eventBuffers.set(sessionId, {
      data,
      score,
      timer: setTimeout(() => commitBufferedEvent(sessionId), EVENT_MERGE_MS),
    });
  }

  function commitBufferedEvent(sessionId) {
    const buf = eventBuffers.get(sessionId);
    if (!buf) return;
    eventBuffers.delete(sessionId);
    const data = buf.data;
    const event = String(data.event || "");
    const now = Date.now();
    lastSessionAt.set(sessionId, now);

    if (bubbleShown) {
      enqueueEvent(data, now, "bubble-visible");
      return;
    }
    if ((now - lastNarrateAt) < NARRATE_THROTTLE_MS) {
      enqueueEvent(data, now, "throttled");
      return;
    }
    if (narrating) {
      enqueueEvent(data, now, "narrating");
      return;
    }
    log(`[narrator] accept event=${event} session=${sessionId.slice(0,8)} agent=${data.agent_id} score=${buf.score}`);
    void dispatchNarration(data);
  }

  function enqueueEvent(data, now, reason) {
    // De-dupe against anything already in the queue with the same session.
    queuedEvents = queuedEvents.filter(q => String(q.data.session_id || "") !== String(data.session_id || ""));
    queuedEvents.push({ data, queuedAt: now });
    while (queuedEvents.length > QUEUE_MAX) queuedEvents.shift();  // drop oldest
    log(`[narrator] enqueue (${reason}): event=${data.event} session=${String(data.session_id||"").slice(0,8)} queue=${queuedEvents.length}/${QUEUE_MAX}`);
  }

  function buildNarrationPrompt(data) {
    const cwdName = (() => {
      const c = String(data.cwd || "");
      const parts = c.split("/").filter(Boolean);
      return parts.length ? parts[parts.length - 1] : "";
    })();
    const niceCwd = cwdName && !cwdName.startsWith("tmp.") ? cwdName : "";
    const isCursor = data.agent_id === "cursor-agent";
    // Two pieces of context populated by the hook script:
    //   title       : conversation topic (first user message)
    //   summary     : what AI did/said in the last reply (truncated)
    const title = typeof data.session_title === "string" && data.session_title.trim()
      ? data.session_title.trim()
      : "";
    const rawSummary = typeof data.last_summary === "string" && data.last_summary.trim()
      ? data.last_summary.trim()
      : (typeof data.assistant_last_output === "string" && data.assistant_last_output.trim()
          ? data.assistant_last_output.trim()
          : "");
    // Cap the summary so system prompt + user prompt + generation stay
    // within the model's 4096-token context window.  System prompt ≈ 700
    // tokens, event template ≈ 75, max_new_tokens = 50 → budget for
    // summary text ≈ 3200 tokens.  At ~1.5 tokens/CJK char (worst case)
    // that's ~2100 chars; use 800 to leave a comfortable margin.
    const SUMMARY_CHAR_LIMIT = 800;
    const summary = rawSummary.length > SUMMARY_CHAR_LIMIT
      ? rawSummary.slice(0, SUMMARY_CHAR_LIMIT) + "…"
      : rawSummary;

    // Build the event description in the user's UI language. The
    // narration system prompt + situation templates live in
    // `minicpm-i18n.js` so non-Chinese users get prompts the model can
    // actually narrate in.
    const lang = getLang();
    const narration = minicpmI18n.getNarration(lang);
    const subject = title
      ? minicpmI18n.makeTranslator(() => lang, minicpmI18n.NARRATION)("subjectQuoted", { title })
      : (niceCwd
          ? minicpmI18n.makeTranslator(() => lang, minicpmI18n.NARRATION)("subjectFromCwd", { cwd: niceCwd })
          : "");
    const tnar = minicpmI18n.makeTranslator(() => lang, minicpmI18n.NARRATION);
    let situation;
    if (data.event === "StopFailure") {
      situation = subject
        ? tnar("eventStopFailureWithSubject", { subject })
        : tnar("eventStopFailureNoSubject");
    } else if (data.event === "Notification") {
      situation = subject
        ? tnar("eventNotificationWithSubject", { subject })
        : tnar("eventNotificationNoSubject");
    } else {
      situation = subject
        ? tnar("eventStopWithSubject", { subject })
        : tnar("eventStopNoSubject");
    }
    if (summary) {
      situation += tnar("eventLastSaid", { summary });
    }

    // Narration always runs the base model (`disable_adapter: true`) so
    // the persona LoRA doesn't bias output toward cuteness over info
    // density.
    return {
      system: narration.systemPrompt,
        user: `Event:${situation}\nReply:`,
    };
  }

  async function dispatchNarration(data) {
    narrating = true;
    lastNarrateAt = Date.now();
    try {
      const prompt = buildNarrationPrompt(data);
      log(`[narrator] dispatch event=${data.event} agent=${data.agent_id} prompt=${JSON.stringify(prompt.user)}`);
      const body = JSON.stringify({
        messages: [{ role: "user", content: prompt.user }],
        system: prompt.system,
        stream: false,
        max_new_tokens: 50,
        thinking: false,
        temperature: 0.7,
        top_p: 0.9,
        repetition_penalty: 1.15,
        silent: true,            // don't push pet animation states for narrator
        disable_adapter: true,   // bypass persona LoRA — narration must be functional/informative
      });
      const r = await httpJson("POST", `${sidecar.baseUrl()}/api/chat`, JSON.parse(body), 30000);
      let text = (r.json && (r.json.content || "")).trim();
        // Strip a "Reply:" prefix the few-shot format may leak (the
        // legacy Chinese-format prefix is still tolerated).
        text = text.replace(/^(?:回复|Reply)[:：]\s*/i, "");
      // First line only — multi-line responses become "thoughts" we don't
      // want to drop into a small bubble.
      text = text.split(/\r?\n/)[0].trim();
      // Strip surrounding quote characters (some models love quoting the reply).
      text = text.replace(/^[「『"']+|[」』"']+$/g, "").trim();
      // Cap to first sentence + ≤50 chars total. Rich enough to convey a
      // concrete result, short enough to fit the bubble at one glance.
      const firstStop = text.search(/[。！？!?]/);
      if (firstStop > 0 && firstStop < text.length - 1) text = text.slice(0, firstStop + 1);
      if (text.length > 50) text = text.slice(0, 49) + "…";
      if (!text) {
        log("[narrator] empty reply, skipping");
        return;
      }
      log(`[narrator] reply: ${text}`);
      showNarrationLine(text, data.event, data.agent_id);
    } catch (err) {
      log(`[narrator] failed: ${err && err.message || err}`);
    } finally {
      narrating = false;
      // Fire next queued event after a short breather (still respects
      // throttle/bubble-visible checks via onStateEvent → eventBuffers).
      // Drop stale entries while we're at it.
      pruneStaleQueue();
      const q = queuedEvents.shift();
      if (q) {
        setTimeout(() => onStateEvent(q.data), 1500);
      }
    }
  }

  function showNarrationLine(text, kind, agent) {
    if (!text) return;
    maybePlayReminderChime();
    ensureBubble();
    reposition();
    if (!bubble || bubble.isDestroyed()) return;
    bubble.webContents.send("minicpm:narrate", { text, kind: kind || "Notification", agent: agent || "" });
    bubble.showInactive();
    bubbleShown = true;
    // Drive the dwell + hide from the main process so it doesn't rely on
    // the renderer's setTimeout (Chromium can throttle timers in hidden
    // panel windows on macOS, which leaves the bubble pinned).
    const dwellMs = Math.max(4000, Math.min(9000, 2400 + text.length * 130));
    setTimeout(() => {
      if (!bubble || bubble.isDestroyed()) return;
      try { bubble.hide(); } catch {}
      bubbleShown = false;
      log(`[narrator] hidden after dwell=${dwellMs}ms`);
      // Replay any queued event that arrived while we were narrating.
      flushQueuedEventIfStale();
    }, dwellMs + 220);
  }

  function setNarrationEnabled(value) {
    const next = !!value;
    if (narrationEnabled !== next) {
      narrationEnabled = next;
      mergeMinicpmPrefs({ narration_enabled: narrationEnabled });
    }
    return narrationEnabled;
  }
  function isNarrationEnabled() { return narrationEnabled; }

  function pruneStaleQueue() {
    const now = Date.now();
    queuedEvents = queuedEvents.filter(q => (now - q.queuedAt) < QUEUED_EVENT_MAX_AGE_MS);
  }

  // When the user closes the chat bubble, drain the queue (oldest first,
  // staggered) so the conversations they missed each get a turn.
  function flushQueuedEventIfStale() {
    pruneStaleQueue();
    const q = queuedEvents.shift();
    if (!q) return;
    onStateEvent(q.data);
  }

  // Eagerly start the Python sidecar in the background so the model and
  // MPS kernels are ready by the time the user clicks the pet. Also probes
  // for a newer model revision once the sidecar is healthy.
  async function warmup() {
    try {
      log("[minicpm-chat] warming up sidecar in background…");
      // Pass the user-effective model dir so the sidecar's `--model` flag
      // tracks Settings changes / Onboarding downloads without restart.
      const r = await sidecar.ensureRunning(getEffectiveModelDir());
      log(`[minicpm-chat] sidecar warmup ${r.status}`);
      void refreshUpdateStatus();
      void refreshPersona();
      // Boot-time address/behavior config push (deduped inside).
      void syncAssistantConfig();
      // Dev UI smoke tests: with the CDP debug port enabled, open the
      // bubble immediately so tools/verify-bubble-ui.mjs can find its
      // target without anyone pressing Ctrl+Shift+M.
      if (process.env.DESKPET_REMOTE_DEBUGGING_PORT) {
        try { await open(); } catch (err) {
          log(`[minicpm-chat] dev auto-open bubble failed: ${err && err.message || err}`);
        }
      }
    } catch (err) {
      log(`[minicpm-chat] sidecar warmup failed: ${err && err.message || err}`);
    }
  }

  async function refreshUpdateStatus() {
    const status = await sidecar.checkUpdate();
    if (!status) return null;
    updateStatus = status;
    log(`[minicpm-chat] update check: local=${status.local_revision || "?"} remote=${status.remote_revision || "?"} available=${status.available}`);
    if (status.available && bubble && !bubble.isDestroyed()) {
      bubble.webContents.send("minicpm:update-status", status);
    }
    return status;
  }

  async function refreshPersona() {
    try {
      const r = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 1500);
      if (r.json && r.json.persona) {
        if (r.json.persona !== activePersona) {
          activePersona = r.json.persona;
          log(`[minicpm-chat] persona = ${activePersona}${r.json.adapter ? " (adapter: " + r.json.adapter + ")" : ""}`);
        }
      }
    } catch {}
  }

  function getUpdateStatus() { return updateStatus; }

  async function applyUpdate(onProgress) {
    // Stream SSE progress back so callers can drive a UI.
    return new Promise((resolve) => {
      const u = new URL(`${sidecar.baseUrl()}/api/update-apply`);
      const req = http.request({
        hostname: u.hostname,
        port: u.port || 80,
        path: u.pathname,
        method: "POST",
        headers: { "content-type": "application/json", "content-length": 0 },
        timeout: 0,
      }, (res) => {
        let buf = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          buf += chunk;
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            if (!block.startsWith("data:")) continue;
            try {
              const ev = JSON.parse(block.slice(5).trim());
              try { onProgress && onProgress(ev); } catch {}
            } catch {}
          }
        });
        res.on("end", () => resolve({ ok: true }));
      });
      req.on("error", (err) => resolve({ ok: false, error: err.message }));
      req.end();
    });
  }

  // ── Context menu (right-click on bubble) ───────────────────────────────

  async function openContextMenu() {
    const m = await sidecar.listModels();
    const items = [];
    if (m && Array.isArray(m.items) && m.items.length) {
      for (const item of m.items) {
        items.push({
          label: item.name,
          type: "checkbox",
          checked: item.path === m.current,
          click: async () => {
            if (item.path === m.current) return;
            bubble.webContents.send("minicpm:cmd-dismiss");
            await sidecar.loadModel(item.path);
            // Re-open in ask mode after model swap so the user can ask the
            // newly loaded model right away.
            await open();
          },
        });
      }
    } else {
      items.push({ label: "(no models found)", enabled: false });
    }
    items.push({ type: "separator" });

    const updLabel = updateStatus
      ? (updateStatus.available
          ? `● Update available: ${updateStatus.remote_revision} → Update now`
          : `Up to date (${updateStatus.local_revision || "?"})`)
      : "Check for model updates";
    items.push({
      label: updLabel,
      enabled: !(updateStatus && updateStatus.busy),
      click: async () => {
        if (updateStatus && updateStatus.available) {
          // Trigger apply with progress, surfacing through the bubble.
          if (bubble && !bubble.isDestroyed()) {
            bubble.webContents.send("minicpm:update-applying", { phase: "start" });
          }
          await applyUpdate((ev) => {
            if (bubble && !bubble.isDestroyed()) {
              bubble.webContents.send("minicpm:update-applying", ev);
            }
          });
          await refreshUpdateStatus();
        } else {
          await refreshUpdateStatus();
          if (bubble && !bubble.isDestroyed()) {
            bubble.webContents.send("minicpm:update-status", updateStatus);
          }
        }
      },
    });

    items.push({ type: "separator" });
    items.push({
      label: `Pet narration (narrate on Stop / errors)`,
      type: "checkbox",
      checked: narrationEnabled,
      click: (it) => { setNarrationEnabled(!!it.checked); },
    });
    items.push({ type: "separator" });
    items.push({
      label: "Clear chat history",
      click: () => { if (bubble && !bubble.isDestroyed()) bubble.webContents.send("minicpm:cmd-reset"); },
    });
    items.push({
      label: "Close bubble",
      click: () => dismiss(),
    });

    const menu = Menu.buildFromTemplate(items);
    if (bubble && !bubble.isDestroyed()) menu.popup({ window: bubble });
  }

  // ── IPC ───────────────────────────────────────────────────────────────

  const handlers = {
    "minicpm:status": async () => ({
      bridgeDir,
      url: sidecar.baseUrl(),
      healthy: await sidecar.isHealthy(getEffectiveModelDir()),
    }),
    "minicpm:start": async (_evt, opts = {}) => {
      try {
        // Default to the user-effective dir; opts.modelDir still wins
        // when callers want a one-off override.
        const r = await sidecar.ensureRunning(opts.modelDir || getEffectiveModelDir());
        return { ok: true, status: r.status, url: sidecar.baseUrl() };
      } catch (err) {
        return { ok: false, error: localizeError(err) };
      }
    },
    "minicpm:get-i18n": async () => {
      const lang = getLang();
      return minicpmI18n.getMinicpmI18nPayload(lang);
    },
    "minicpm:get-assistant-prefs": async () => getAssistantPrefsSnapshot(),
    "minicpm:resize": (_evt, { width, height } = {}) => {
      width = Math.max(MIN_WIDTH, Math.min(SPEAK_MAX_WIDTH, Math.round(Number(width) || ASK_WIDTH)));
      height = Math.max(MIN_HEIGHT, Math.min(SPEAK_MAX_HEIGHT, Math.round(Number(height) || ASK_HEIGHT)));
      chooseAndApplyBounds(width, height);
      return { ok: true, width, height };
    },
    "minicpm:set-chat-anchor": (_evt, { bottomY } = {}) => {
      // Renderer enters/exits "anchor-bottom while typing" mode. Pass null
      // to clear and go back to default center anchor.
      chatAnchorBottomY = (typeof bottomY === "number" && Number.isFinite(bottomY)) ? bottomY : null;
      return { ok: true };
    },
    "minicpm:hide-window": () => {
      if (bubble && !bubble.isDestroyed() && bubble.isVisible()) bubble.hide();
      bubbleShown = false;
      // Bubble closed → if a coding-agent event was queued during chat,
      // replay it now (subject to the 60s freshness window).
      setTimeout(() => flushQueuedEventIfStale(), 600);
      return { ok: true };
    },
    "minicpm:update-status": async () => {
      // Returns the cached status + triggers a fresh background refresh.
      void refreshUpdateStatus();
      return updateStatus || { available: false };
    },
    "minicpm:update-apply": async () => {
      // Stream progress events back to the renderer in real time so the UI
      // can paint the progress bar; resolve the invoke once the apply is
      // finished.
      const result = await applyUpdate((ev) => {
        if (bubble && !bubble.isDestroyed()) {
          bubble.webContents.send("minicpm:update-applying", ev);
        }
      });
      await refreshUpdateStatus();
      return { ...result, status: updateStatus };
    },
    "minicpm:focus-window": () => {
      // Bring bubble to the front AND give it keyboard focus. Used when
      // we transition back to ask mode after a reply so the user can
      // type immediately without re-clicking the pet.
      if (bubble && !bubble.isDestroyed()) {
        try {
          if (!bubble.isVisible()) bubble.show();
          else bubble.show(); // also raises macOS panel to key window
          bubble.focus();
          bubbleShown = true;
        } catch (err) { log(`[minicpm-chat] focus failed: ${err.message}`); }
      }
      return { ok: true };
    },
    "minicpm:show-window": () => {
      bubbleShown = true;
      if (bubble && !bubble.isDestroyed() && !bubble.isVisible()) {
        // Re-pick the side based on current pet position before showing,
        // so the bubble pops back next to the pet even if it moved while
        // the bubble was hidden.
        const pb = ctx.getPetWindowBounds && ctx.getPetWindowBounds();
        const wa = pb ? (ctx.getNearestWorkArea
          ? ctx.getNearestWorkArea(pb.x + pb.width / 2, pb.y + pb.height / 2)
          : screen.getPrimaryDisplay().workArea) : null;
        if (pb && wa) {
          const { width, height } = bubble.getBounds();
          activeSide = pickSide(pb, wa, width, height, bubblePos.side);
          bubble.setBounds(computeBubbleBoundsForSide(activeSide, pb, wa, width, height, {
            offsetDx: bubblePos.dx, offsetDy: bubblePos.dy,
          }));
        }
        bubble.showInactive();
      }
      return { ok: true };
    },
  };
  for (const [ch, fn] of Object.entries(handlers)) {
    try { ipcMain.removeHandler(ch); } catch {}
    ipcMain.handle(ch, fn);
  }

  ipcMain.removeAllListeners("minicpm:open-context-menu");
  ipcMain.on("minicpm:open-context-menu", () => { void openContextMenu(); });

  try { ipcMain.removeHandler("minicpm:get-chat-params"); } catch {}
  ipcMain.handle("minicpm:get-chat-params", async () => getChatParams());

  // ── Chat history persistence IPC ──────────────────────────────────────
  // v2: both directions carry the whole session store — ONE load + ONE
  // save call, written atomically (temp file + rename).
  try { ipcMain.removeHandler("minicpm-chat:history-load"); } catch {}
  ipcMain.handle("minicpm-chat:history-load", async () => {
    try {
      if (!fs.existsSync(HISTORY_PATH)) return { ok: true, store: normalizeHistoryStore(null) };
      const parsed = JSON.parse(fs.readFileSync(HISTORY_PATH, "utf-8"));
      return { ok: true, store: normalizeHistoryStore(parsed) };
    } catch (err) {
      log(`[minicpm] history load failed: ${err && err.message}`);
      return { ok: false, store: normalizeHistoryStore(null) };
    }
  });

  try { ipcMain.removeHandler("minicpm-chat:history-save"); } catch {}
  ipcMain.handle("minicpm-chat:history-save", async (_e, payload) => {
    try {
      const raw = payload && typeof payload === "object" && payload.store !== undefined
        ? payload.store
        : payload && payload.history; // legacy renderer payload shape
      const clean = normalizeHistoryStore(raw);
      let count = 0;
      for (const sess of Object.values(clean.sessions)) count += sess.messages.length;
      const tmp = `${HISTORY_PATH}.tmp`;
      fs.writeFileSync(
        tmp,
        JSON.stringify({
          version: 2,
          saved_at: new Date().toISOString(),
          activeId: clean.activeId,
          sessions: clean.sessions,
        }, null, 2),
        "utf-8"
      );
      // Atomic on Windows too: rename replaces an existing destination.
      fs.renameSync(tmp, HISTORY_PATH);
      return { ok: true, count, sessions: Object.keys(clean.sessions).length };
    } catch (err) {
      log(`[minicpm] history save failed: ${err && err.message}`);
      return { ok: false };
    }
  });

  // ── Proactive drawer IPC (tasks + memory → gateway REST) ──────────────
  // Thin proxy: the renderer stays CSP-clean (no direct fetch to :18765)
  // and the gateway stays the single source of truth for both stores.
  const gw = (method, path, body, timeoutMs) =>
    httpJson(method, `${sidecar.baseUrl()}${path}`, body === undefined ? null : body, timeoutMs || 4000)
      .then((r) => (r && r.json) || { ok: false, error: r ? `HTTP ${r.status}` : "no response" })
      .catch((err) => ({ ok: false, error: (err && err.message) || String(err) }));

  try { ipcMain.removeHandler("minicpm:tasks-list"); } catch {}
  ipcMain.handle("minicpm:tasks-list", () => gw("GET", "/api/tasks"));

  try { ipcMain.removeHandler("minicpm:tasks-create"); } catch {}
  ipcMain.handle("minicpm:tasks-create", (_e, p) => {
    const a = (p && p.args) || p || {};
    const name = String(a.name || "").trim().slice(0, 80);
    const delay = Number(a.delaySeconds);
    if (!name || !Number.isFinite(delay) || delay <= 0) {
      return Promise.resolve({ ok: false, error: "name and a positive delaySeconds are required" });
    }
    return gw("POST", "/api/tasks", {
      name,
      delay_seconds: Math.min(delay, 60 * 60 * 24 * 30), // cap at 30 days
      payload: String(a.payload || ""),
      recurring: !!a.recurring,
    }, 6000);
  });

  try { ipcMain.removeHandler("minicpm:tasks-delete"); } catch {}
  ipcMain.handle("minicpm:tasks-delete", (_e, p) => {
    const id = String((p && p.id) || "").trim();
    return id ? gw("DELETE", `/api/tasks/${encodeURIComponent(id)}`) : Promise.resolve({ ok: false });
  });

  try { ipcMain.removeHandler("minicpm:memory-list"); } catch {}
  ipcMain.handle("minicpm:memory-list", () => gw("GET", "/api/memory"));

  try { ipcMain.removeHandler("minicpm:memory-add"); } catch {}
  ipcMain.handle("minicpm:memory-add", (_e, p) => {
    const text = String((p && p.text) || "").trim().slice(0, 2000);
    return text
      ? gw("POST", "/api/memory", { text }, 6000)
      : Promise.resolve({ ok: false, error: "text is required" });
  });

  try { ipcMain.removeHandler("minicpm:memory-delete"); } catch {}
  ipcMain.handle("minicpm:memory-delete", (_e, p) => {
    const id = String((p && p.id) || "").trim();
    return id ? gw("DELETE", `/api/memory/${encodeURIComponent(id)}`) : Promise.resolve({ ok: false });
  });

  try { ipcMain.removeHandler("minicpm:memory-search"); } catch {}
  ipcMain.handle("minicpm:memory-search", (_e, p) => {
    const q = String((p && p.q) || "").trim();
    return q ? gw("POST", "/api/memory/search", { query: q }, 6000) : Promise.resolve({ ok: false });
  });

  // ── Settings-window facing IPC ────────────────────────────────────────
  // Surface the MiniCPM panel state to the main Settings window.
  const settingsHandlers = {
    "minicpm-settings:get-status": async () => {
      // /api/health internally chains a call into llama-server's /health, so
      // a too-tight timeout falsely paints a live sidecar as "offline" the
      // moment llama is briefly busy (KV flush after a chat, model swap,
      // adapter load, etc). 5s keeps the probe still cheap but resilient to
      // that micro-jitter.
      const health = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 5000).catch(() => null);
      const llamaReady = !!(health && health.json && (
        health.json.alive === true ||
        (health.json.llama_server && health.json.llama_server.status === "ok")
      ));
      const requireLlama = isModelPresent();
      const sidecarReady = !!(health && health.json && health.json.ok);
      return {
        sidecarUrl: sidecar.baseUrl(),
        healthy: !!(sidecarReady && (!requireLlama || llamaReady)),
        sidecarReady,
        llamaReady,
        health: health ? health.json : null,
        narration: narrationEnabled,
      };
    },
    "minicpm-settings:list-adapters": async () => {
      // Gateway is the source of truth for the *physical* adapter set
      // (which files exist, persona slug, current active). The manifest
      // adds product-layer metadata (displayName, aliases, source,
      // entry id). We join by absolute path so renames + uploads in
      // the same dir resolve correctly.
      const r = await httpJson("GET", `${sidecar.baseUrl()}/api/adapters`, null, 2000).catch(() => null);
      const remote = r && r.json ? r.json : { items: [], current: null, current_name: null };
      const manifest = readAdapterManifest();
      const byPath = new Map();
      for (const item of manifest.items || []) {
        if (!item || !item.path) continue;
        try { byPath.set(path.resolve(item.path), item); }
        catch { byPath.set(item.path, item); }
      }
      const remoteItems = Array.isArray(remote.items) ? remote.items : [];
      const merged = remoteItems.map((g) => {
        let entry = null;
        try { entry = byPath.get(path.resolve(g.path)) || null; }
        catch { entry = null; }
        return {
          ...g,
          id: entry && entry.id ? entry.id : `external:${g.path}`,
          displayName: entry && entry.displayName ? entry.displayName : g.name,
          aliases: entry && Array.isArray(entry.aliases) ? entry.aliases : [],
          source: entry && entry.source ? entry.source : "external",
        };
      });
      // Surface manifest entries whose .gguf went missing too, so the
      // user can clean them up from the UI rather than wondering why
      // their preset vanished.
      const remoteSet = new Set();
      for (const g of remoteItems) {
        try { remoteSet.add(path.resolve(g.path)); } catch { remoteSet.add(g.path); }
      }
      for (const entry of manifest.items || []) {
        if (!entry || !entry.path) continue;
        const key = (() => { try { return path.resolve(entry.path); } catch { return entry.path; } })();
        if (remoteSet.has(key)) continue;
        merged.push({
          name: path.basename(entry.path),
          path: entry.path,
          persona: entry.persona || "default",
          id: entry.id,
          displayName: entry.displayName || path.basename(entry.path),
          aliases: Array.isArray(entry.aliases) ? entry.aliases : [],
          source: entry.source || "external",
          missing: true,
        });
      }
      return {
        ...remote,
        items: merged,
      };
    },
    "minicpm-settings:load-adapter": async (_evt, payload) => {
      const requested = (payload && payload.path) || null;
      // Short-circuit when the requested adapter is already active —
      // skip the load-adapter call entirely (it's a few-hundred-ms op
      // even when no-op) and don't wipe chat history.
      const cur = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 1500).catch(() => null);
      const currentAdapter = cur && cur.json ? (cur.json.adapter || null) : undefined;
      const sameAdapter = currentAdapter !== undefined && (
        (requested === null && !currentAdapter) ||
        (requested && currentAdapter && requested === currentAdapter)
      );
      if (sameAdapter) {
        const personaName = cur.json.persona && cur.json.persona !== "default" ? cur.json.persona : null;
        const adapterName = (currentAdapter && String(currentAdapter).split("/").pop()) || null;
        let text;
        if (!currentAdapter) text = "Already on the base model.";
        else if (personaName) text = `Already on LoRA · ${personaName}.`;
        else text = `LoRA · ${adapterName || "?"} is already loaded.`;
        try {
          ensureBubble();
          reposition();
          if (!bubble.isVisible()) bubble.showInactive();
          bubbleShown = true;
          bubble.webContents.send("minicpm:cmd-reply", { text, ok: true });
        } catch (err) {
          log(`[minicpm] adapter no-op notify failed: ${err && err.message}`);
        }
        return { ok: true, noop: true, adapter: currentAdapter, persona: cur.json.persona };
      }

      const r = await httpJson("POST", `${sidecar.baseUrl()}/api/load-adapter`, payload || {}, 90000).catch(() => null);
      const data = r ? r.json : null;
      // Persist the user's choice so the next sidecar spawn loads
      // exactly this LoRA via --lora (or no --lora at all when
      // path is null). We resolve the manifest entry by path so the
      // stored id stays in sync even after rename / re-import.
      if (data && data.ok) {
        try {
          if (!requested) {
            setActiveAdapterId(null);
          } else {
            const manifest = readAdapterManifest();
            const entry = (manifest.items || []).find((it) => {
              try { return it && it.path && path.resolve(it.path) === path.resolve(requested); }
              catch { return false; }
            });
            setActiveAdapterId(entry ? entry.id : null);
          }
          refreshActiveAdapterPath();
        } catch (err) {
          log(`[minicpm] persist active adapter failed: ${err && err.message}`);
        }
        // Mirror the in-chat command UX: pop a fade-out reply bubble next
        // to the pet announcing the swap, and tell the renderer to wipe
        // its conversation history so the new persona starts clean.
        const personaName = data.persona && data.persona !== "default" ? data.persona : null;
        const adapterName = (data.adapter && String(data.adapter).split("/").pop()) || null;
        let text;
        if (!data.adapter) text = "Switched back to the base model; chat history cleared.";
        else if (personaName) text = `Switched to LoRA · ${personaName}; chat history cleared.`;
        else text = `Loaded LoRA · ${adapterName || "?"}; chat history cleared.`;
        try {
          ensureBubble();
          reposition();
          if (!bubble.isVisible()) bubble.showInactive();
          bubbleShown = true;
          bubble.webContents.send("minicpm:cmd-reply", { text, ok: true, resetHistory: true });
        } catch (err) {
          log(`[minicpm] adapter swap notify failed: ${err && err.message}`);
        }
      }
      return data;
    },
    "minicpm-settings:check-update": async () => {
      const r = await httpJson("GET", `${sidecar.baseUrl()}/api/update-check`, null, 5000).catch(() => null);
      return r ? r.json : null;
    },
    "minicpm-settings:apply-update": async () => {
      // Reuse the same update path the bubble menu uses; results in events
      // streamed via the chat bubble (if open).
      try {
        const result = await applyUpdate(() => {});
        await refreshUpdateStatus();
        return { ...result, status: updateStatus };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },
    "minicpm-settings:set-narration": async (_evt, payload) => {
      setNarrationEnabled(!!(payload && payload.enabled));
      return { ok: true, enabled: narrationEnabled };
    },

    // ── Accelerator / device manual override ────────────────────────────
    "minicpm-settings:list-devices": async () => {
      const r = await httpJson("GET", `${sidecar.baseUrl()}/api/devices`, null, 2000).catch(() => null);
      return r ? r.json : null;
    },
    "minicpm-settings:set-device": async (_evt, payload) => {
      const device = (payload && payload.device) || "";
      if (device === "vulkan" && process.platform !== "win32") {
        return { ok: false, device, error: "Vulkan backend is only configurable on Windows" };
      }
      // Persist for the next sidecar spawn even if /api/set-device is
      // unreachable (sidecar may have crashed). MINICPM_DEVICE is the
      // single source of truth our server.py reads at start.
      process.env.MINICPM_DEVICE = device;
      try {
        await httpJson("POST", `${sidecar.baseUrl()}/api/set-device`, { device }, 1500);
      } catch {}
      return { ok: true, device, note: "Takes effect next time the server restarts" };
    },
    "minicpm-settings:set-device-and-restart": async (_evt, payload) => {
      const device = (payload && payload.device) || "";
      if (device === "vulkan" && process.platform !== "win32") {
        return { ok: false, device, error: "Vulkan backend is only configurable on Windows" };
      }
      const previousDevice = process.env.MINICPM_DEVICE || "";
      process.env.MINICPM_DEVICE = device;
      try {
        await sidecar.stopAndWait();
      } catch (stopErr) {
        process.env.MINICPM_DEVICE = previousDevice;
        return { ok: false, device, phase: "stop", error: localizeError(stopErr) };
      }
      try {
        const r = await sidecar.ensureRunning(getEffectiveModelDir());
        return { ok: true, device, status: r && r.status };
      } catch (err) {
        if (process.platform === "win32" && device === "vulkan") {
          const originalError = localizeError(err);
          process.env.MINICPM_DEVICE = "cpu";
          try {
            await sidecar.stopAndWait();
          } catch (fallbackStopErr) {
            return {
              ok: false,
              device,
              phase: "fallback-stop",
              error: `${originalError}; CPU fallback cleanup failed: ${localizeError(fallbackStopErr)}`,
            };
          }
          try {
            const r = await sidecar.ensureRunning(getEffectiveModelDir());
            log(`[minicpm-chat] Vulkan backend failed; fell back to CPU: ${originalError}`);
            return { ok: false, fallback: "cpu", device: "cpu", status: r && r.status, error: originalError };
          } catch (fallbackErr) {
            return { ok: false, fallback: "cpu", device: "cpu", error: localizeError(fallbackErr) };
          }
        }
        return { ok: false, device, error: localizeError(err) };
      }
    },
    "minicpm-settings:restart-sidecar": async () => {
      try {
        await sidecar.stopAndWait();
        const r = await sidecar.ensureRunning(getEffectiveModelDir());
        return { ok: true, status: r && r.status };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },

    // ── Local model directory override ──────────────────────────────────
    "minicpm-settings:get-model-dir": async () => ({
      current: getEffectiveModelDir(),
      default: getDefaultModelDir(),
      present: isModelPresent(),
    }),
    "minicpm-settings:pick-model-dir": async () => {
      const { dialog } = require("electron");
      const ret = await dialog.showOpenDialog({
        title: "Select local Deskpet model (folder containing .gguf)",
        properties: ["openFile", "openDirectory"],
        filters: [{ name: "GGUF model", extensions: ["gguf"] }],
        message: "Can be a single .gguf file, or a directory containing a .gguf file",
      });
      if (ret.canceled || !ret.filePaths.length) return { ok: false, canceled: true };
      const picked = ret.filePaths[0];
      let target = picked;
      try {
        const st = fs.statSync(picked);
        if (st.isDirectory()) {
          const entries = fs.readdirSync(picked)
            .filter((n) => n.toLowerCase().endsWith(".gguf"));
          if (!entries.length) {
            return { ok: false, error: `The selected directory does not contain a .gguf file:\n${picked}` };
          }
          target = path.join(picked, entries[0]);
        } else if (!picked.toLowerCase().endsWith(".gguf")) {
          return { ok: false, error: `Please select a .gguf file:\n${picked}` };
        }
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
      setEffectiveModelDir(target);
      // Persisting the prefs only takes effect on the next sidecar spawn —
      // hot-swap via /api/load-model so the running llama-server actually
      // picks up the new .gguf without a manual restart. swap_model on the
      // gateway stops + respawns llama-server with the new --model and
      // blocks until the /health probe returns 200, so a successful
      // resolution here means the new model is already serving requests.
      let reloadError = null;
      try {
        const r = await sidecar.loadModel(target);
        if (r && r.error) reloadError = String(r.error);
      } catch (err) {
        reloadError = String(err && err.message || err);
      }
      return { ok: true, modelDir: target, reloaded: !reloadError, reloadError };
    },
    "minicpm-settings:open-model-dir": async () => {
      const health = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 1500).catch(() => null);
      const gguf = resolveCurrentGgufPath(health ? health.json : null);
      if (gguf) {
        try {
          shell.showItemInFolder(gguf);
          return { ok: true, path: gguf, highlighted: true };
        } catch (err) {
          return { ok: false, error: String(err && err.message || err) };
        }
      }
      const dir = getEffectiveModelDir();
      try {
        fs.mkdirSync(dir, { recursive: true });
        const err = await shell.openPath(dir);
        if (err) return { ok: false, error: err };
        return { ok: true, dir, highlighted: false };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },

    // ── Adapter (LoRA) directory ────────────────────────────────────────
    // Same shape as the model dir handlers: read the effective path,
    // open it in Finder/Explorer (creating it if missing), and let the
    // Settings tab refresh after the user drops new .gguf files in.
    "minicpm-settings:get-adapter-dir": async () => ({
      current: getEffectiveAdapterDir(),
      default: getDefaultAdapterDir(),
    }),
    "minicpm-settings:open-adapter-dir": async () => {
      const dir = getEffectiveAdapterDir();
      try {
        fs.mkdirSync(dir, { recursive: true });
        const err = await shell.openPath(dir);
        if (err) return { ok: false, error: err };
        return { ok: true, dir };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },

    // ── Adapter manifest CRUD ───────────────────────────────────────────
    // The Settings UI reads the merged list via `list-adapters` (which
    // joins gateway items with manifest metadata) and mutates the
    // manifest through these handlers. After every mutation we also
    // refresh the gateway's mirror (handled inside writeAdapterManifest)
    // so chat-bubble keyword routing — which goes straight to the
    // sidecar HTTP API — sees the updated displayName / aliases on the
    // next /api/adapters call.

    "minicpm-settings:get-adapter-manifest": async () => readAdapterManifest(),

    "minicpm-settings:upload-adapter": async (_evt, payload) => {
      const { dialog } = require("electron");
      const ret = await dialog.showOpenDialog({
        title: "Select LoRA adapter (.gguf)",
        properties: ["openFile"],
        filters: [{ name: "GGUF adapter", extensions: ["gguf"] }],
        message: "Pick a GGUF-format LoRA adapter file; it will be copied into the app's adapters directory.",
      });
      if (ret.canceled || !ret.filePaths.length) {
        return { ok: false, canceled: true };
      }
      const src = ret.filePaths[0];
      const lower = src.toLowerCase();
      if (!lower.endsWith(".gguf")) {
        return { ok: false, error: `Not a .gguf file: ${src}` };
      }
      let srcStat;
      try { srcStat = fs.statSync(src); }
      catch (err) { return { ok: false, error: `Cannot read the selected file: ${err && err.message}` }; }
      if (!srcStat.isFile()) {
        return { ok: false, error: `Not a regular file: ${src}` };
      }
      const ts = Date.now();
      const safeBasename = path.basename(src).replace(/[^A-Za-z0-9._-]+/g, "_");
      const uploadsDir = path.join(getEffectiveAdapterDir(), "uploads");
      const destName = `${ts}_${safeBasename}`;
      const dest = path.join(uploadsDir, destName);
      try { fs.mkdirSync(uploadsDir, { recursive: true }); }
      catch (err) { return { ok: false, error: `Cannot create uploads directory: ${err && err.message}` }; }
      try {
        fs.copyFileSync(src, dest);
      } catch (err) {
        return { ok: false, error: `Failed to copy file: ${err && err.message}` };
      }
      const displayName = (payload && typeof payload.displayName === "string" && payload.displayName.trim())
        ? payload.displayName.trim()
        : path.basename(src, ".gguf");
      const aliases = Array.isArray(payload && payload.aliases)
        ? payload.aliases.map((s) => String(s || "").trim()).filter(Boolean)
        : [];
      const entry = upsertAdapterEntry({
        id: `upload:${ts}`,
        path: dest,
        displayName,
        aliases,
        persona: "custom",
        source: "user-upload",
      });
      return { ok: true, item: entry };
    },

    "minicpm-settings:rename-adapter": async (_evt, payload) => {
      if (!payload || typeof payload.id !== "string") {
        return { ok: false, error: "id is required" };
      }
      const manifest = readAdapterManifest();
      const idx = manifest.items.findIndex((it) => it && it.id === payload.id);
      if (idx < 0) return { ok: false, error: `adapter not found: ${payload.id}` };
      const patch = {};
      if (typeof payload.displayName === "string") patch.displayName = payload.displayName.trim() || manifest.items[idx].displayName;
      if (Array.isArray(payload.aliases)) {
        patch.aliases = payload.aliases.map((s) => String(s || "").trim()).filter(Boolean);
      }
      const merged = upsertAdapterEntry({ ...manifest.items[idx], ...patch });
      return { ok: true, item: merged };
    },

    "minicpm-settings:remove-adapter": async (_evt, payload) => {
      if (!payload || typeof payload.id !== "string") {
        return { ok: false, error: "id is required" };
      }
      const manifest = readAdapterManifest();
      const target = manifest.items.find((it) => it && it.id === payload.id);
      if (!target) return { ok: false, error: `adapter not found: ${payload.id}` };
      if (target.source !== "user-upload") {
        return { ok: false, error: "Only LoRAs you uploaded yourself can be deleted; bundled ones stay." };
      }
      // If the user just removed the currently active adapter, unload
      // it on the sidecar side first so llama-server's per-request
      // `lora` array doesn't reference a path we're about to unlink.
      try {
        const health = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 1500).catch(() => null);
        const currentPath = health && health.json && health.json.adapter ? String(health.json.adapter) : null;
        if (currentPath && target.path && path.resolve(currentPath) === path.resolve(target.path)) {
          await httpJson("POST", `${sidecar.baseUrl()}/api/load-adapter`, { path: null }, 30000).catch(() => null);
        }
      } catch {}
      if (payload.deleteFile && target.path) {
        try { fs.unlinkSync(target.path); }
        catch (err) { log(`[minicpm] adapter file unlink failed: ${err && err.message}`); }
      }
      removeAdapterEntry(payload.id);
      return { ok: true, id: payload.id };
    },
    "minicpm-settings:get-resources": async () => {
      const root = sidecar.proc && sidecar.proc.pid;
      if (!root) return { ok: false, reason: "no-sidecar" };
      try {
        const all = await listAllProcesses();
        const tree = collectProcessTree(root, all);
        const total_rss_kb = tree.reduce((sum, p) => sum + (p.rss || 0), 0);
        const total_cpu = tree.reduce((sum, p) => sum + (p.cpu || 0), 0);
        const health = await httpJson("GET", `${sidecar.baseUrl()}/api/health`, null, 1500).catch(() => null);
        const h = health && health.json ? health.json : {};
        const gguf_path = resolveCurrentGgufPath(h);
        let gguf_size = null;
        if (gguf_path) {
          try { gguf_size = fs.statSync(gguf_path).size; } catch {}
        }
        const llama = tree.find((p) => /llama-server/i.test(p.cmd));
        const ctx_size = Number(process.env.MINICPM_CTX) || 4096;
        const mmap_kb = gguf_size ? Math.round(gguf_size / 1024) : null;
        const private_kb = mmap_kb != null
          ? Math.max(0, total_rss_kb - mmap_kb)
          : total_rss_kb;
        return {
          ok: true,
          total_rss_kb,
          total_cpu,
          private_kb,
          mmap_kb,
          gguf_size,
          gguf_path,
          ctx_size,
          accel: h.accel || h.device || null,
          backend: h.backend || null,
          llama_alive: !!(h.alive || (h.llama_server && h.llama_server.status === "ok")),
          processes: tree.map((p) => ({
            pid: p.pid,
            rss: p.rss,
            cpu: p.cpu,
            cmd: p.cmd.slice(0, 160),
          })),
          llama_pid: llama ? llama.pid : null,
        };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },
    "minicpm-settings:reset-model-dir": async () => {
      setEffectiveModelDir(null);
      return { ok: true, modelDir: getDefaultModelDir() };
    },

    // ── Onboarding rerun (dev / recovery) ───────────────────────────────
    "minicpm-settings:rerun-onboarding": async () => {
      // Delete the sentinel and tell main.js to relaunch. main.js will
      // see shouldShow()===true on next boot and open the wizard.
      try {
        const sentinelPath = path.join(app.getPath("userData"), "minicpm-onboarding.json");
        if (fs.existsSync(sentinelPath)) fs.unlinkSync(sentinelPath);
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
      // Don't call app.relaunch() here directly — the renderer expects an
      // explicit "yes I want to restart" confirmation. The handler just
      // marks the file; the Settings UI shows a "重启应用" button afterwards.
      return { ok: true };
    },
    "minicpm-settings:relaunch-app": async () => {
      // Hard-restart so the new sentinel state takes effect cleanly.
      setTimeout(() => {
        app.relaunch();
        app.quit();
      }, 100);
      return { ok: true };
    },

    // ── Logs (sidecar.log + crash dumps) ────────────────────────────────
    "minicpm-settings:get-logs-info": async () => {
      const entries = [];
      try {
        for (const name of fs.readdirSync(logsDir)) {
          try {
            const st = fs.statSync(path.join(logsDir, name));
            entries.push({
              name,
              size: st.size,
              mtime: st.mtime.toISOString(),
            });
          } catch {}
        }
      } catch {}
      entries.sort((a, b) => (a.mtime < b.mtime ? 1 : -1));
      return {
        dir: logsDir,
        sidecarLog: sidecarLogPath,
        entries,
      };
    },
    "minicpm-settings:open-logs-dir": async () => {
      const { shell } = require("electron");
      try { fs.mkdirSync(logsDir, { recursive: true }); } catch {}
      try {
        const err = await shell.openPath(logsDir);
        if (err) return { ok: false, error: err };
        return { ok: true, dir: logsDir };
      } catch (err) {
        return { ok: false, error: String(err && err.message || err) };
      }
    },

    "minicpm-settings:get-chat-params": async () => ({
      params: getChatParams(),
      defaults: { ...DEFAULT_CHAT_PARAMS },
    }),
    "minicpm-settings:set-chat-params": async (_evt, payload) => ({
      ok: true,
      params: setChatParams(payload && payload.params),
    }),
    "minicpm-settings:reset-chat-params": async () => ({
      ok: true,
      params: setChatParams(DEFAULT_CHAT_PARAMS),
    }),

    "minicpm-settings:get-bubble-pos": async () => ({
      pos: getBubblePos(),
      defaults: { ...DEFAULT_BUBBLE_POS },
      editing: bubbleEditing,
    }),
    "minicpm-settings:set-bubble-pos": async (_evt, payload) => {
      const next = setBubblePos(payload && payload.pos);
      // Reposition immediately if the bubble is currently open so the
      // change is visible without forcing the user to reopen it.
      try { reposition(); } catch {}
      return { ok: true, pos: next };
    },
    "minicpm-settings:reset-bubble-pos": async () => {
      const next = setBubblePos(DEFAULT_BUBBLE_POS);
      try { reposition(); } catch {}
      return { ok: true, pos: next };
    },
    // Drag-to-position flow:
    //   1. Settings calls "enter-bubble-edit". We open the bubble next
    //      to the pet, swap it into a draggable sample, and pause any
    //      narration / auto-hide while the user fiddles with it.
    //   2. User drags the OS window around (the renderer applies
    //      -webkit-app-region: drag to the whole body in edit mode).
    //   3. Settings calls "exit-bubble-edit" with `save: true` to
    //      capture the final offset, or `save: false` to discard.
    "minicpm-settings:enter-bubble-edit": async () => {
      try {
        ensureBubble();
        bubbleEditing = true;
        // Apply the saved side preference so what the user is editing
        // matches what they'll see at runtime.
        const pb = getPetBoundsSafe();
        const wa = pb ? getWorkAreaForPet(pb) : screen.getPrimaryDisplay().workArea;
        if (pb) {
          activeSide = pickSide(pb, wa, ASK_WIDTH, ASK_HEIGHT, bubblePos.side);
          bubble.setBounds(computeBubbleBoundsForSide(activeSide, pb, wa, ASK_WIDTH, ASK_HEIGHT, {
            offsetDx: bubblePos.dx, offsetDy: bubblePos.dy,
          }));
        }
        if (!bubble.isVisible()) bubble.showInactive();
        bubbleShown = true;
        bubble.webContents.send("minicpm:edit-mode", { enabled: true, side: activeSide });
        return { ok: true, side: activeSide };
      } catch (err) {
        bubbleEditing = false;
        return { ok: false, error: String(err && err.message || err) };
      }
    },
    "minicpm-settings:exit-bubble-edit": async (_evt, payload) => {
      const save = !!(payload && payload.save);
      let savedPos = getBubblePos();
      try {
        if (save && bubble && !bubble.isDestroyed()) {
          const pb = getPetBoundsSafe();
          const wa = pb ? getWorkAreaForPet(pb) : screen.getPrimaryDisplay().workArea;
          const actual = bubble.getBounds();
          if (pb && wa) {
            // Compute defaults at offset 0 to derive the user's drag delta.
            const def = computeBubbleBoundsForSide(activeSide, pb, wa, actual.width, actual.height, {
              offsetDx: 0, offsetDy: 0,
            });
            let dx = 0;
            if (activeSide === "left") dx = def.x - actual.x;
            else if (activeSide === "right") dx = actual.x - def.x;
            else dx = actual.x - def.x;
            const dy = actual.y - def.y;
            savedPos = setBubblePos({ ...bubblePos, dx, dy });
          }
        }
      } finally {
        bubbleEditing = false;
        try {
          if (bubble && !bubble.isDestroyed()) {
            bubble.webContents.send("minicpm:edit-mode", { enabled: false });
            bubble.hide();
            bubbleShown = false;
          }
        } catch {}
      }
      return { ok: true, saved: save, pos: savedPos };
    },
  };
  for (const [ch, fn] of Object.entries(settingsHandlers)) {
    try { ipcMain.removeHandler(ch); } catch {}
    ipcMain.handle(ch, fn);
  }

  // Stop the running sidecar (if any) and immediately restart it. Used
  // after settings changes that the engine reads at construction time
  // only — accelerator (MINICPM_DEVICE) and the active model directory.
  async function restartSidecar() {
    await sidecar.stopAndWait();
    return sidecar.ensureRunning(getEffectiveModelDir());
  }

  // Boot or attach to the sidecar and wait until /api/health returns
  // ok. Unlike `warmup()`, this surface bubbles failures upwards — the
  // Onboarding wizard needs to *know* if spawn failed so it can show a
  // proper error message instead of hitting ECONNREFUSED later on.
  async function ensureSidecarReady() {
    return sidecar.ensureRunning(getEffectiveModelDir());
  }

  function sendI18n() {
    if (!bubble || bubble.isDestroyed()) return;
    try {
      bubble.webContents.send(
        "minicpm:lang-change",
        minicpmI18n.getMinicpmI18nPayload(getLang())
      );
    } catch {}
  }

  return {
    open,
    toggle,
    dismiss,
    toggleThinking,
    warmup,
    onStateEvent,
    setNarrationEnabled,
    isNarrationEnabled,
    setPetDragging,
    isOpen: () => bubbleShown && !!(bubble && !bubble.isDestroyed()),
    reposition,
    shutdown,
    restartSidecar,
    ensureSidecarReady,
    syncAssistantConfig,
    getAssistantPrefsSnapshot,
    sendI18n,
    getSidecarUrl: () => sidecar.baseUrl(),
    getBridgeDir: () => bridgeDir,
    getSidecarBinary: () => sidecarBin,
    getLogsDir: () => logsDir,
    getSidecarLogPath: () => sidecarLogPath,
    // Model directory introspection — consumed by Onboarding + Settings.
    getModelDir: () => getEffectiveModelDir(),
    getDefaultModelDir,
    setModelDir: (dir) => setEffectiveModelDir(dir),
    isModelPresent: () => isModelPresent(),
  };
};
