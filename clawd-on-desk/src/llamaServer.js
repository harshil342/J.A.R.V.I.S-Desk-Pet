"use strict";

const { spawn, execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");
const { app } = require("electron");

function httpJson(method, urlStr, timeoutMs = 4000) {
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
    req.end();
  });
}

function resolveGgufPath(dirOrFile) {
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

class LlamaServerManager {
  constructor(port = 18766, log = console.log) {
    this.port = port;
    this.host = "127.0.0.1";
    this.proc = null;
    this.starting = null;
    this.log = log;
    this.binPath = this._locateBinary();
  }

  _locateBinary() {
    const ext = process.platform === "win32" ? ".exe" : "";
    let candidates = [];
    if (app && app.isPackaged) {
      candidates = [
        path.join(process.resourcesPath, "bin", "llama-server" + ext),
        path.join(process.resourcesPath, "llama-server" + ext),
      ];
    } else {
      const appRoot = path.resolve(__dirname, "..");
      candidates = [
        path.join(appRoot, "bin", "llama-server" + ext),
      ];
    }
    
    for (const c of candidates) {
      try {
        if (fs.statSync(c).isFile()) return c;
      } catch {}
    }
    return null;
  }

  baseUrl() {
    return `http://${this.host}:${this.port}`;
  }

  async isHealthy() {
    try {
      const r = await httpJson("GET", `${this.baseUrl()}/health`, 1500);
      return r.status === 200 && r.json && r.json.status === "ok";
    } catch {
      return false;
    }
  }

  async ensureRunning(modelDir) {
    if (await this.isHealthy()) return { status: "already-running" };
    if (this.starting) return this.starting;

    this.starting = this._spawnAndWait(modelDir).finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  async _spawnAndWait(modelDir) {
    if (!this.binPath) {
      throw new Error(`llama-server executable not found. Please place llama-server in the bin directory.`);
    }

    const gguf = resolveGgufPath(modelDir);
    if (!gguf) {
      throw new Error(`No .gguf model found in ${modelDir}`);
    }

    const args = [
      "-m", gguf,
      "--host", this.host,
      "--port", String(this.port),
      "-c", "4096"
    ];

    this.log(`[llama-server] Spawning ${this.binPath} ${args.join(" ")}`);
    const proc = spawn(this.binPath, args, {
      cwd: path.dirname(this.binPath)
    });
    
    this.proc = proc;

    proc.stdout.on("data", (b) => this.log(`[llama-server] ${b.toString().trimEnd()}`));
    proc.stderr.on("data", (b) => this.log(`[llama-server] ${b.toString().trimEnd()}`));

    proc.on("exit", (code, signal) => {
      this.log(`[llama-server] exited with code=${code} signal=${signal}`);
      if (this.proc === proc) this.proc = null;
    });

    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      if (!this.proc) {
        throw new Error("llama-server process exited prematurely.");
      }
      if (await this.isHealthy()) {
        this.log(`[llama-server] server is healthy!`);
        return { status: "started" };
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    
    this.stop();
    throw new Error("Timed out waiting for llama-server to start.");
  }

  stop() {
    if (!this.proc) return;
    const proc = this.proc;
    
    if (process.platform === "win32" && proc.pid) {
      try {
        execFile("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { windowsHide: true }, () => {});
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

    if (!proc) return;

    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        resolve();
      };
      proc.once("exit", finish);
      setTimeout(finish, timeoutMs);
    });
  }

  async checkUpdate() { return null; }
  async listModels() { return null; }
  async loadModel(target) {
    try {
      await this.stopAndWait();
      const r = await this.ensureRunning(target);
      return { ok: true, status: r && r.status };
    } catch (err) {
      return { ok: false, error: String(err && err.message || err) };
    }
  }
}

module.exports = { LlamaServerManager };
