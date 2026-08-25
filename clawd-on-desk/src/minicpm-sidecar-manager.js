"use strict";

// Sidecar locating (binary / dev sources / Python interpreter), adapter
// manifest + bundled-seed helpers, a tiny HTTP helper, and the Sidecar
// process manager class. Extracted from minicpm-chat.js, which re-imports
// every exported name so its eval-based tests keep binding them.
const { execFile, spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
// Electron's app is needed for userData paths; test harnesses stub the
// "electron" resolver before loading, so this resolves to their stub too.
const { app } = require("electron");

// ── locate sidecar binary / dev sources / Python interpreter ───────────────
//
// Two runtime modes, in priority order:
//   A. Packaged app   → bundled binary at <resourcesPath>/sidecar-bin/
//                         minicpm-sidecar(.exe)   ← PyInstaller gateway
//                         llama-server(.exe)      ← llama.cpp build product
//                       (the only path real users ever hit)
//   B. Dev with venv  → minicpm-sidecar/.venv/bin/python -m gateway
//                       (created by `uv sync` inside minicpm-sidecar/)
//
// MINICPM_SIDECAR_BIN / MINICPM_SIDECAR_DIR / MINICPM_PYTHON env vars
// override every mode for local debugging.

function locateSidecarBinary(appRoot) {
  const override = process.env.MINICPM_SIDECAR_BIN;
  if (override && fs.existsSync(override)) return path.resolve(override);
  const ext = process.platform === "win32" ? ".exe" : "";
  if (app && app.isPackaged) {
    // electron-builder puts the binary under
    //   <Contents>/Resources/sidecar-bin/         (macOS .app bundle)
    //   <install>/resources/sidecar-bin/          (Windows / Linux)
    const candidates = [
      path.join(process.resourcesPath, "sidecar-bin", "minicpm-sidecar" + ext),
      path.join(process.resourcesPath, "sidecar-bin", "minicpm-sidecar", "minicpm-sidecar" + ext),
    ];
    for (const c of candidates) {
      try { if (fs.statSync(c).isFile()) return c; } catch {}
    }
  }
  // Dev convenience: scripts/build-gateway.sh emits binaries under
  //   <repo>/minicpm-sidecar/bin/<os>-<arch>/minicpm-sidecar
  // so devs can dogfood the production codepath without rebuilding
  // electron-builder every time.
  const triple = triplet();
  const devBin = path.join(appRoot, "..", "minicpm-sidecar", "bin", triple, "minicpm-sidecar" + ext);
  try { if (fs.statSync(devBin).isFile()) return devBin; } catch {}
  return null;
}

function locateSidecarSourceDir(appRoot) {
  const override = process.env.MINICPM_SIDECAR_DIR;
  if (override) {
    try {
      if (fs.statSync(path.join(override, "gateway", "__main__.py")).isFile()) {
        return path.resolve(override);
      }
    } catch {}
  }
  const candidates = [];
  if (app && app.isPackaged) {
    // Packaged builds ship the source next to the binary so a dev
    // override at MINICPM_PYTHON still has somewhere to point at.
    candidates.push(path.join(process.resourcesPath, "minicpm-sidecar"));
  }
  candidates.push(path.join(appRoot, "..", "minicpm-sidecar"));
  for (const c of candidates) {
    try {
      if (fs.statSync(path.join(c, "gateway", "__main__.py")).isFile()) {
        return path.resolve(c);
      }
    } catch {}
  }
  return null;
}

function locatePython(sidecarDir) {
  // 1. Explicit override always wins.
  const explicit = process.env.MINICPM_PYTHON;
  if (explicit && fs.existsSync(explicit)) return explicit;

  if (!sidecarDir) return null;
  const venvCandidates = [
    path.join(sidecarDir, ".venv", "bin", "python"),
    path.join(sidecarDir, ".venv", "bin", "python3"),
    path.join(sidecarDir, ".venv", "Scripts", "python.exe"),
  ];
  for (const p of venvCandidates) {
    try { if (fs.statSync(p).isFile()) return p; } catch {}
  }
  return null;
}

function triplet() {
  // Matches electron-builder's `${os}-${arch}` expansion so extraResources
  // paths and our dev bin/<triple>/ layout line up.
  const arch = process.arch === "arm64" ? "arm64" : process.arch === "x64" ? "x64" : process.arch;
  if (process.platform === "darwin") return "mac-"   + arch;
  if (process.platform === "win32")  return "win-"   + arch;
  if (process.platform === "linux")  return "linux-" + arch;
  return process.platform + "-" + arch;
}

// ── Adapter manifest pure helpers ──────────────────────────────────────
//
// These work on plain JS objects with no IO so they're easy to unit-test
// without mocking Electron's `app`. The closure-level wrappers inside
// `initMinicpmChat` do the actual fs reads / writes and call into here.

function parseManifestJson(text) {
  try {
    const raw = JSON.parse(text);
    if (!raw || typeof raw !== "object" || !Array.isArray(raw.items)) {
      return { version: 1, items: [] };
    }
    return {
      version: Number(raw.version) || 1,
      items: raw.items.filter((it) => it && typeof it === "object"),
    };
  } catch {
    return { version: 1, items: [] };
  }
}

function manifestUpsertItem(items, entry) {
  if (!entry || !entry.id) return Array.isArray(items) ? items.slice() : [];
  const out = Array.isArray(items) ? items.slice() : [];
  const idx = out.findIndex((it) => it && it.id === entry.id);
  if (idx >= 0) {
    out[idx] = { ...out[idx], ...entry };
  } else {
    out.push({ createdAt: new Date().toISOString(), ...entry });
  }
  return out;
}

function manifestRemoveItem(items, id) {
  const out = Array.isArray(items) ? items.filter((it) => it && it.id !== id) : [];
  return out;
}

// ── Bundled-preset reconcile pure helpers ──────────────────────────────
//
// When a shipped bundle replaces a preset adapter with a newer build (a
// fresh timestamped dir for the same persona), the old copy can linger in
// <userData>/adapters/ after the new one is seeded in. Both .gguf get the
// same persona slug from filename hints, so Settings shows the persona
// twice — and because only one is in the manifest, the other falls back
// to its raw `adapter_model.f16.gguf` filename. These pure helpers decide
// what to re-point / delete; the closure wrapper does the fs walk + writes.

// Mirror of findAdapterByHint's match rule: the hint hits when it's a
// substring of the filename OR its immediate parent dir name (case-
// insensitive). The parent-dir check matters because the .gguf is usually
// generically named while the persona lives in the dir name.
function adapterMatchesHint(filePath, hint) {
  if (!filePath || !hint) return false;
  const needle = String(hint).toLowerCase();
  const lower = path.basename(filePath).toLowerCase();
  const parent = path.basename(path.dirname(filePath)).toLowerCase();
  return lower.includes(needle) || parent.includes(needle);
}

// Per bundled preset, pick the canonical on-disk .gguf and flag older
// copies as superseded. Pure: caller supplies the scanned file list (with
// mtime), the presets, and the current manifest items.
//
//   scanned       : [{ path, name, mtimeMs }]
//   presets       : DEFAULT_PRESET_ENTRIES ({ id, filenameHint, ... })
//   manifestItems : current manifest items
//
// Returns { repoint: [{ id, path }], superseded: [filePath] }. A hint-
// matching file claimed by a *different* manifest entry (e.g. a user
// `upload:*`) is protected: never a candidate, so never re-pointed away
// or deleted.
function planBundledReconcile({ scanned, presets, manifestItems } = {}) {
  const repoint = [];
  const superseded = [];
  const files = Array.isArray(scanned) ? scanned : [];
  const presetList = Array.isArray(presets) ? presets : [];
  const items = Array.isArray(manifestItems) ? manifestItems : [];
  const resolve = (p) => { try { return path.resolve(p); } catch { return p; } };

  for (const preset of presetList) {
    if (!preset || !preset.id || !preset.filenameHint) continue;
    const protectedPaths = new Set();
    for (const it of items) {
      if (!it || !it.path || it.id === preset.id) continue;
      protectedPaths.add(resolve(it.path));
    }
    const candidates = files.filter(
      (f) => f && f.path &&
        adapterMatchesHint(f.path, preset.filenameHint) &&
        !protectedPaths.has(resolve(f.path)),
    );
    if (candidates.length === 0) continue;
    // Canonical = newest by mtime; tie-broken by greatest path so the
    // timestamped dir name (…20260524…) wins deterministically.
    const canonical = candidates.slice().sort((a, b) => {
      const am = Number(a.mtimeMs) || 0;
      const bm = Number(b.mtimeMs) || 0;
      if (am !== bm) return bm - am;
      return a.path < b.path ? 1 : a.path > b.path ? -1 : 0;
    })[0];
    const current = items.find((it) => it && it.id === preset.id);
    if (current && current.path && resolve(current.path) !== resolve(canonical.path)) {
      repoint.push({ id: preset.id, path: canonical.path });
    }
    for (const c of candidates) {
      if (resolve(c.path) !== resolve(canonical.path)) superseded.push(c.path);
    }
  }
  return { repoint, superseded };
}

// Guard for the destructive step: map a superseded .gguf to what may be
// safely removed. Never returns a target at or above the adapter root.
//   - file in a proper subdir of adapterDir → delete that subdir
//   - file directly in adapterDir           → delete just the file
//   - file == adapterDir / outside it        → skip
function safeDeleteTargetFor(filePath, adapterDir) {
  if (!filePath || !adapterDir) return { kind: "skip", target: null };
  let file, root;
  try { file = path.resolve(filePath); root = path.resolve(adapterDir); }
  catch { return { kind: "skip", target: null }; }
  const parent = path.dirname(file);
  if (parent === root) return { kind: "file", target: file };
  if (parent.startsWith(root + path.sep)) return { kind: "dir", target: parent };
  return { kind: "skip", target: null };
}

// Recursive copy of bundled LoRA adapters from `srcDir` (where
// electron-builder dropped them via extraResources) into `dstDir`
// (the user-writable `<userData>/adapters/` we point the gateway at).
//
// Idempotent: skips any file that already exists at the destination so
// user deletions stick across app restarts. Only copies the file kinds
// the gateway and Settings UI care about (`.gguf` weights + README /
// adapter_config metadata), to keep the user dir tidy.
//
// Returns `{ copied, skipped, errors }` for log + test introspection.
// Failures on individual files don't abort the walk — we want best
// effort, the worst case is the user just doesn't see the default
// nekoqa preset and has to drop the .gguf in by hand.
function seedAdaptersFromBundle(srcDir, dstDir, fsImpl = fs, log = () => {}) {
  const result = { copied: [], skipped: [], errors: [] };
  if (!srcDir) return result;
  try { fsImpl.mkdirSync(dstDir, { recursive: true }); } catch {}

  function walk(curSrc, curDst) {
    let entries;
    try { entries = fsImpl.readdirSync(curSrc, { withFileTypes: true }); }
    catch { return; }
    for (const entry of entries) {
      const s = path.join(curSrc, entry.name);
      const d = path.join(curDst, entry.name);
      if (entry.isDirectory()) {
        try { fsImpl.mkdirSync(d, { recursive: true }); } catch {}
        walk(s, d);
        continue;
      }
      if (!entry.isFile()) continue;
      const lower = entry.name.toLowerCase();
      const isAllowed =
        lower.endsWith(".gguf") ||
        lower.endsWith(".md") ||
        lower === "adapter_config.json";
      if (!isAllowed) continue;
      try {
        if (fsImpl.existsSync(d)) {
          result.skipped.push(d);
          continue;
        }
        fsImpl.copyFileSync(s, d);
        result.copied.push(d);
      } catch (err) {
        const msg = err && err.message ? err.message : String(err);
        log(`[minicpm] adapter seed copy failed: ${entry.name} -> ${msg}`);
        result.errors.push({ path: d, error: msg });
      }
    }
  }
  try { walk(srcDir, dstDir); }
  catch (err) {
    const msg = err && err.message ? err.message : String(err);
    log(`[minicpm] seedAdaptersFromBundle walk failed: ${msg}`);
    result.errors.push({ path: dstDir, error: msg });
  }
  return result;
}

// ── HTTP probe helpers ──────────────────────────────────────────────────────

function httpJson(method, urlStr, body, timeoutMs = 4000) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const opts = {
      hostname: u.hostname,
      port: u.port || 80,
      path: u.pathname + (u.search || ""),
      method,
      headers: { "content-type": "application/json" },
      timeout: timeoutMs,
    };
    const req = http.request(opts, (res) => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode || 0, json: data ? JSON.parse(data) : null });
        } catch {
          resolve({ status: res.statusCode || 0, json: null, raw: data });
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// ── Sidecar manager ─────────────────────────────────────────────────────────

class Sidecar {
  constructor({ sidecarDir, sidecarBin, appRoot, port, host, log, logFile, adapterDir, modelPresent, onUnexpectedExit }) {
    // Source tree of minicpm-sidecar; used only in dev when no prebuilt
    // binary is present. Packaged builds ignore it entirely.
    this.sidecarDir = sidecarDir || null;
    // Optional prebuilt gateway binary. When set we skip Python lookup.
    // Populated in packaged builds via electron-builder extraResources →
    // resources/sidecar-bin/minicpm-sidecar[.exe].
    this.sidecarBin = sidecarBin || null;
    this.appRoot = appRoot || null;
    this.port = port;
    this.host = host;
    this.log = log || (() => {});
    this.proc = null;
    this.starting = null;
    this.stderrTail = [];
    // Called with (code, signal) when the process dies without a planned
    // stop() — crash, OOM kill, external taskkill. The owner wires an
    // auto-restart backoff to this (see factory below).
    this.onUnexpectedExit = typeof onUnexpectedExit === "function" ? onUnexpectedExit : null;
    // True between stop()/stopAndWait() and the exit event — lets the
    // exit handler tell "we killed it" from "it died on its own".
    this._stopping = false;
    // Where the gateway should scan for *.gguf LoRA adapters. We pass
    // it via MINICPM_ADAPTER_DIR env at spawn time so /api/adapters and
    // /api/load-adapter see the same directory Settings → "open adapter
    // folder" exposes to the user.
    this.adapterDir = adapterDir || null;
    // Mutable: which LoRA (if any) the user wants loaded at this
    // sidecar's startup. We re-read prefs each respawn so a swap done
    // via Settings persists across an explicit "Restart Sidecar".
    this.activeAdapterPath = null;
    // Append-mode file stream where every stdout / stderr line from the
    // sidecar gets persisted to <userData>/logs/sidecar.log. Critical
    // for packaged builds where console.log goes nowhere.
    this.logFile = logFile || null;
    this._fileStream = null;
    this._fileSizeBudget = 2 * 1024 * 1024; // 2 MB before rotate
    this._fileBytesWritten = 0;
    this.modelPresent = typeof modelPresent === "function" ? modelPresent : (() => false);
  }

  _openLogStream() {
    if (!this.logFile) return null;
    if (this._fileStream) return this._fileStream;
    try {
      fs.mkdirSync(path.dirname(this.logFile), { recursive: true });
      // Pre-rotate if the existing file is already over budget so we
      // start clean each app launch (or restart of the sidecar).
      try {
        const st = fs.statSync(this.logFile);
        if (st.size > this._fileSizeBudget) {
          fs.renameSync(this.logFile, this.logFile + ".1");
        }
      } catch {}
      this._fileStream = fs.createWriteStream(this.logFile, { flags: "a" });
      this._fileBytesWritten = 0;
      const ts = new Date().toISOString();
      this._fileStream.write(`\n===== sidecar session ${ts} (host=${this.host} port=${this.port}) =====\n`);
    } catch (err) {
      this.log(`[minicpm-chat] open log file failed: ${err && err.message}`);
    }
    return this._fileStream;
  }

  _appendLog(line) {
    const stream = this._openLogStream();
    if (!stream) return;
    try {
      const chunk = line.endsWith("\n") ? line : line + "\n";
      stream.write(chunk);
      this._fileBytesWritten += Buffer.byteLength(chunk);
      // Soft rotate: when the stream grows past budget, roll over once.
      // We do this lazily so we don't fsync on every line.
      if (this._fileBytesWritten > this._fileSizeBudget) {
        try {
          stream.end();
          fs.renameSync(this.logFile, this.logFile + ".1");
        } catch {}
        this._fileStream = null;
        this._fileBytesWritten = 0;
      }
    } catch {}
  }

  // Pull last N stderr chunks (raw) for inclusion in error toasts /
  // crash dumps.
  _stderrTailString(maxChars = 1500) {
    return (this.stderrTail.join("").trim().slice(-maxChars)) || "(no stderr)";
  }

  baseUrl() { return `http://${this.host}:${this.port}`; }

  async ensureRunning(initialModelDir) {
    if (await this.isHealthy(initialModelDir)) return { status: "already-running" };

    // Gateway may be running but without a loaded model (alive=false).
    // Hot-load via /api/load-model instead of spawning a second process
    // which would fail with EADDRINUSE on the same port.
    if (this.proc && this.modelPresent(initialModelDir)) {
      const gguf = this._resolveGgufPath(initialModelDir);
      if (gguf) {
        const loaded = await this.loadModel(gguf);
        if (loaded && loaded.ok) return { status: "model-loaded" };
      }
    }

    if (this.starting) return this.starting;
    this.starting = this._spawnAndWait(initialModelDir).finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  _resolveGgufPath(dirOrFile) {
    try {
      const st = fs.statSync(dirOrFile);
      if (st.isFile() && dirOrFile.toLowerCase().endsWith(".gguf")) return dirOrFile;
      if (st.isDirectory()) {
        const entries = fs.readdirSync(dirOrFile)
          .filter((n) => n.toLowerCase().endsWith(".gguf"));
        if (entries.length) return path.join(dirOrFile, entries[0]);
      }
    } catch {}
    return null;
  }

  async isHealthy(initialModelDir) {
    try {
      const r = await httpJson("GET", `${this.baseUrl()}/api/health`, null, 1500);
      if (!(r.status === 200 && r.json && r.json.ok === true)) return false;
      if (this.modelPresent(initialModelDir)) {
        return r.json.alive === true
          || !!(r.json.llama_server && r.json.llama_server.status === "ok");
      }
      return true;
    } catch {
      return false;
    }
  }

  async listModels() {
    try {
      const r = await httpJson("GET", `${this.baseUrl()}/api/models`, null, 2000);
      return r.json || null;
    } catch { return null; }
  }

  async loadModel(p) {
    try {
      const r = await httpJson("POST", `${this.baseUrl()}/api/load-model`, { path: p }, 90000);
      return r.json || null;
    } catch (err) { return { error: String(err && err.message || err) }; }
  }

  async checkUpdate() {
    try {
      const r = await httpJson("GET", `${this.baseUrl()}/api/update-check`, null, 4000);
      return r.json || null;
    } catch { return null; }
  }

  async _spawnAndWait(initialModelDir) {
    // We need either the prebuilt gateway binary or the source tree
    // (with a Python venv) to spawn.
    if (!this.sidecarBin && !this.sidecarDir) {
      const err = new Error("sidecar binary not found");
      err.minicpmI18nKey = "chatSidecarMissingBin";
      throw err;
    }

    // Both the binary and `python -m gateway` accept the same flags;
    // we treat them uniformly here.
    const argsCommon = [
      "--host", this.host,
      "--port", String(this.port),
    ];
    if (initialModelDir) argsCommon.push("--model", initialModelDir);

    const env = {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      // Windows: the gateway's "auto" device resolves to CPU. If a CUDA
      // llama-server backend ships under bin/<triple>/backends/cuda/ and
      // the user hasn't pinned a device, default to cuda so NVIDIA
      // machines get GPU inference out of the box.
      MINICPM_DEVICE: process.env.MINICPM_DEVICE
        || (process.platform === "win32" && fs.existsSync(path.join(
          this.sidecarDir || "", "bin", "win-x64", "backends", "cuda", "llama-server.exe"
        )) ? "cuda" : "auto"),
      // Mirror our sidecar.log directory into the gateway so its
      // RotatingFileHandler drops sidecar-internal.log next to what
      // Electron captures — easy to grab via Settings → "打开日志目录".
      MINICPM_LOG_DIR: this.logFile ? path.dirname(this.logFile) : (process.env.MINICPM_LOG_DIR || ""),
      // Point gateway at the writable user adapter dir so /api/adapters
      // and /api/load-adapter see exactly what Settings UI shows.
      MINICPM_ADAPTER_DIR: this.adapterDir || process.env.MINICPM_ADAPTER_DIR || "",
      // Boot directly into the user's persisted LoRA choice. Empty
      // string (or unset) means "boot Base, no LoRA loaded" — the
      // gateway then refrains from passing any --lora flag, keeping
      // memory minimal for users who never opt in to a persona.
      MINICPM_ACTIVE_ADAPTER: this.activeAdapterPath || process.env.MINICPM_ACTIVE_ADAPTER || "",
      // Pin the parent-watchdog inside the gateway to OUR pid (Electron
      // main), not to whatever ppid the PyInstaller bootloader's Python
      // re-exec hop ends up with. If Electron crashes or is `kill -9`'d,
      // the watchdog notices our pid is gone and tears down the
      // sidecar + llama-server within ~2s, so :18765 / :18766 don't
      // stay held by an orphan.
      MINICPM_PARENT_PID: String(process.pid),
    };

    // Strip proxy environment variables to avoid socksio dependency issues.
    // The sidecar only makes local HTTP calls (localhost:18766) and downloads
    // from HuggingFace (which has its own proxy handling via huggingface_hub).
    const proxyVars = [
      "http_proxy", "https_proxy", "ftp_proxy", "socks_proxy",
      "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "SOCKS_PROXY",
      "all_proxy", "ALL_PROXY",
    ];
    for (const v of proxyVars) {
      delete env[v];
    }

    let proc;
    if (this.sidecarBin) {
      // Production path: a self-contained gateway binary. No Python
      // interpreter required on the host. The gateway itself locates
      // and spawns the llama-server binary sitting next to it.
      this.log(`[minicpm-chat] spawn binary ${this.sidecarBin} --port ${this.port}`);
      proc = spawn(this.sidecarBin, argsCommon, {
        cwd: path.dirname(this.sidecarBin),
        env,
      });
    } else {
      const python = locatePython(this.sidecarDir);
      if (!python) {
        const err = new Error("Python interpreter not found");
        err.minicpmI18nKey = "chatSidecarMissingPython";
        throw err;
      }
      this.log(`[minicpm-chat] spawn ${python} -m gateway --port ${this.port}`);
      proc = spawn(python, ["-m", "gateway", ...argsCommon], {
        cwd: this.sidecarDir,
        env,
      });
    }

    this.proc = proc;
    this._stopping = false;
    this.stderrTail.length = 0;

    // Make sure the log file is open for the new session.
    this._openLogStream();
    this._appendLog(`[spawn] ${this.sidecarBin || "python"} (pid=${proc.pid})`);

    proc.stdout.on("data", (b) => {
      const s = b.toString();
      this.log(`[sidecar] ${s.trimEnd()}`);
      this._appendLog(`[stdout] ${s.trimEnd()}`);
    });
    proc.stderr.on("data", (b) => {
      const s = b.toString();
      this.log(`[sidecar! ] ${s.trimEnd()}`);
      this._appendLog(`[stderr] ${s.trimEnd()}`);
      this.stderrTail.push(s);
      if (this.stderrTail.length > 40) this.stderrTail.shift();
    });
    proc.on("exit", (code, signal) => {
      this.log(`[minicpm-chat] sidecar exited code=${code} signal=${signal}`);
      this._appendLog(`[exit] code=${code} signal=${signal}`);
      const planned = this._stopping;
      // If the process died with a non-zero exit (and wasn't a clean
      // SIGTERM from our own stop()), archive the recent stderr tail as
      // a standalone crash dump so we can investigate after restart.
      const crashed = (typeof code === "number" && code !== 0) ||
                       (signal && signal !== "SIGTERM");
      if (crashed && this.logFile) {
        try {
          const dir = path.dirname(this.logFile);
          const ts = new Date().toISOString().replace(/[:.]/g, "-");
          const dump = path.join(dir, `sidecar-crash-${ts}.log`);
          const header =
            `# sidecar crash dump\n` +
            `# at:    ${new Date().toISOString()}\n` +
            `# code:  ${code}\n` +
            `# sig:   ${signal}\n` +
            `# pid:   ${proc.pid}\n` +
            `# bin:   ${this.sidecarBin || "python"}\n` +
            `# port:  ${this.port}\n` +
            `\n----- stderr tail -----\n`;
          fs.writeFileSync(dump, header + this._stderrTailString(8000), "utf-8");
          // Prune to the 5 most recent crash dumps.
          try {
            const files = fs.readdirSync(dir)
              .filter((f) => f.startsWith("sidecar-crash-"))
              .sort()
              .reverse();
            for (const old of files.slice(5)) {
              try { fs.unlinkSync(path.join(dir, old)); } catch {}
            }
          } catch {}
          this.log(`[minicpm-chat] crash dump → ${dump}`);
        } catch (err) {
          this.log(`[minicpm-chat] failed to write crash dump: ${err && err.message}`);
        }
      }
      if (this.proc === proc) this.proc = null;
      // Unplanned death (crash / kill) → give the owner a chance to
      // restart. Planned stop()s are suppressed via the _stopping flag.
      if (!planned && typeof this.onUnexpectedExit === "function") {
        try { this.onUnexpectedExit(code, signal); }
        catch (err) {
          this.log(`[minicpm-chat] onUnexpectedExit handler failed: ${err && err.message}`);
        }
      }
    });

    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      if (!this.proc) {
        const err = new Error(`Python process exited prematurely. stderr tail:\n${this._stderrTailString(1500)}`);
        err.minicpmI18nKey = "chatSidecarPyExited";
        err.minicpmI18nParams = { tail: this._stderrTailString(1500) };
        throw err;
      }
      const health = await httpJson("GET", `${this.baseUrl()}/api/health`, null, 1500).catch(() => null);
      if (health && health.status === 200 && health.json && health.json.ok === true) {
        if (this.modelPresent(initialModelDir)) {
          if (health.json.startup_error) {
            this.stop();
            throw new Error(`llama-server failed to start: ${health.json.startup_error}`);
          }
          if (
            health.json.alive === true ||
            (health.json.llama_server && health.json.llama_server.status === "ok")
          ) {
            return { status: "started" };
          }
        } else {
          return { status: "started" };
        }
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    this.stop();
    const err = new Error("Timed out waiting for Python service (90s)");
    err.minicpmI18nKey = "chatSidecarTimeout";
    throw err;
  }

  stop() {
    this._stopping = true;
    if (!this.proc) return;
    const proc = this.proc;
    const pid = proc.pid;

    if (process.platform === "win32" && pid) {
      // PyInstaller --onefile spawns a bootloader (the pid we get back from
      // child_process.spawn) which then launches the actual Python process
      // as a separate child. Windows doesn't put them in the same job
      // object, so a plain `proc.kill("SIGTERM")` only terminates the
      // bootloader — the Python child stays alive holding the gateway
      // socket on :18765, which then blocks every subsequent respawn with
      // EADDRINUSE / "llama-server not running". Use taskkill /T to walk
      // the process tree and kill the bootloader + every descendant
      // (Python, llama-server, ...) in one shot.
      try {
        execFile("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true }, () => {});
      } catch {
        try { proc.kill("SIGKILL"); } catch {}
      }
      return;
    }

    try { proc.kill("SIGTERM"); } catch {}
    setTimeout(() => {
      if (this.proc === proc) { try { proc.kill("SIGKILL"); } catch {} }
    }, 2000).unref();
  }

  async stopAndWait(timeoutMs = 5000) {
    const proc = this.proc;
    this.stop();

    const waitForProcExit = async () => {
      if (!proc || proc.exitCode != null || proc.signalCode != null) return true;
      return new Promise((resolve) => {
        let done = false;
        let timer = null;
        const finish = (exited) => {
          if (done) return;
          done = true;
          try { proc.removeListener("exit", onExit); } catch {}
          if (timer) clearTimeout(timer);
          resolve(exited);
        };
        const onExit = () => finish(true);
        proc.once("exit", onExit);
        timer = setTimeout(() => finish(false), timeoutMs);
        if (timer && typeof timer.unref === "function") timer.unref();
      });
    };

    const waitForHealthDown = async (deadline) => {
      let misses = 0;
      while (Date.now() < deadline) {
        const r = await httpJson("GET", `${this.baseUrl()}/api/health`, null, 300).catch(() => null);
        if (r && r.status === 200 && r.json && r.json.ok === true) {
          misses = 0;
        } else {
          misses += 1;
          if (misses >= 2) return true;
        }
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
      return false;
    };

    if (!(await waitForProcExit())) {
      throw new Error("Timed out waiting for sidecar process to exit");
    }
    if (!(await waitForHealthDown(Date.now() + timeoutMs))) {
      throw new Error("Timed out waiting for sidecar port to close");
    }
  }
}

// ── Bubble positioning ──────────────────────────────────────────────────────


module.exports = {
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
};
