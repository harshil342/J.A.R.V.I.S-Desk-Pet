#!/usr/bin/env node
// UI smoke test runner: spawns DeskPet with CDP enabled, waits for it,
// runs verify-bubble-ui.mjs against the real bubble, then kills the app.
//
// Usage: node tools/run-ui-smoke.mjs   (exit 0 = every check passed)
//
// ponytail: local/GitHub-Actions-with-GPU harness only — CI runners have
// no MiniCPM model, so wire this into a hosted workflow when one exists.

import { spawn } from "node:child_process";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = process.env.DESKPET_REMOTE_DEBUGGING_PORT || "9222";

function cdpReady() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: Number(PORT), path: "/json/version", timeout: 1500 },
      (res) => { res.resume(); resolve(res.statusCode === 200); },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function bubbleTargetPresent() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: Number(PORT), path: "/json/list", timeout: 1500 },
      (res) => {
        let body = "";
        res.on("data", (c) => { body += c; });
        res.on("end", () => {
          try {
            const targets = JSON.parse(body);
            resolve(targets.some(
              (t) => t.type === "page" && String(t.url || "").includes("minicpm-chat.html"),
            ));
          } catch { resolve(false); }
        });
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

async function waitCdp(seconds) {
  for (let i = 0; i < seconds; i++) {
    if (await cdpReady()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

// The bubble auto-opens only after the sidecar warmup settles, which can
// lag well behind the CDP endpoint — poll for the real page target.
async function waitBubble(seconds) {
  for (let i = 0; i < seconds; i++) {
    if (await bubbleTargetPresent()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

const app = spawn(process.execPath, ["launch.js"], {
  cwd: root,
  env: { ...process.env, DESKPET_REMOTE_DEBUGGING_PORT: PORT },
  stdio: "ignore",
});

let verifier;
try {
  console.log(`[smoke] app pid ${app.pid}, waiting for CDP :${PORT}…`);
  if (!(await waitCdp(120))) {
    throw new Error(`CDP did not come up on :${PORT}`);
  }
  console.log("[smoke] CDP ready — waiting for chat bubble target…");
  if (!(await waitBubble(120))) {
    throw new Error("chat bubble page never appeared on CDP within 120s");
  }
  console.log("[smoke] bubble ready — running verify-bubble-ui.mjs");
  verifier = spawn(process.execPath, ["tools/verify-bubble-ui.mjs"], {
    cwd: root,
    env: { ...process.env, DESKPET_REMOTE_DEBUGGING_PORT: PORT },
    stdio: "inherit",
  });
  const code = await new Promise((resolve) => verifier.on("close", resolve));
  process.exitCode = code ?? 1;
} catch (err) {
  console.error(`[smoke] FAILED: ${err.message || err}`);
  process.exitCode = 1;
} finally {
  // Kill the whole Electron tree; plain kill leaves GPU helpers alive.
  if (process.platform === "win32") {
    spawn("taskkill", ["/PID", String(app.pid), "/T", "/F"], { stdio: "ignore" });
  } else {
    app.kill("SIGTERM");
  }
}
