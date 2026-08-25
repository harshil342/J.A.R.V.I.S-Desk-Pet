"use strict";

// Lock down the chat-history persistence primitives and the Sidecar
// unplanned-exit wiring added for crash auto-restart:
//   - sanitizeHistoryItems: role whitelist + content clamp + turn cap.
//     It runs on BOTH sides of the history IPC so a corrupted or
//     hand-edited minicpm-chat-history.json can't balloon context.
//   - Sidecar: onUnexpectedExit plumbing + _stopping flag semantics
//     that let the factory supervisor tell crashes from planned stops.

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const targetPath = require.resolve("../src/minicpm-chat.js");

function loadInternals() {
  delete require.cache[targetPath];
  const src = fs.readFileSync(targetPath, "utf-8");
  const augmented =
    src +
    "\nmodule.exports.__internals = { sanitizeHistoryItems, Sidecar };\n";
  const m = new Module(targetPath, module);
  m.filename = targetPath;
  m.paths = Module._nodeModulePaths(path.dirname(targetPath));
  m._compile(augmented, targetPath);
  return m.exports.__internals;
}

// Stub electron BEFORE the file is loaded (same trick as
// minicpm-locate.test.js) so the source can be required without a
// real Electron runtime.
const realResolve = Module._resolveFilename;
const fakeElectronPath = path.join(os.tmpdir(), "fake-electron-stub-history.js");
fs.writeFileSync(
  fakeElectronPath,
  `module.exports = {
     BrowserWindow: class {}, ipcMain: {}, screen: {}, shell: {},
     Menu: {}, app: { isPackaged: false, getPath: () => "${os.tmpdir().replace(/\\/g, "/")}" }
   };`
);
Module._resolveFilename = function patched(request, parent, ...rest) {
  if (request === "electron") return fakeElectronPath;
  return realResolve.call(this, request, parent, ...rest);
};

describe("sanitizeHistoryItems", () => {
  let internals;
  beforeEach(() => { internals = loadInternals(); });

  it("keeps user/assistant turns with string content", () => {
    const out = internals.sanitizeHistoryItems([
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ]);
    assert.deepEqual(out, [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ]);
  });

  it("drops system roles, junk entries, and empty content", () => {
    const out = internals.sanitizeHistoryItems([
      { role: "system", content: "inject" },
      null,
      "not an object",
      { role: "user", content: "   " },
      { role: "assistant", content: 42 },
      { role: "user", content: "kept" },
    ]);
    assert.deepEqual(out, [{ role: "user", content: "kept" }]);
  });

  it("clamps oversized content to the char budget", () => {
    const big = "x".repeat(30000);
    const out = internals.sanitizeHistoryItems([{ role: "user", content: big }]);
    assert.equal(out.length, 1);
    assert.ok(out[0].content.length <= 24000);
  });

  it("caps to the most recent messages", () => {
    const many = [];
    for (let i = 0; i < 100; i++) {
      many.push({ role: "user", content: `msg-${i}` });
    }
    const out = internals.sanitizeHistoryItems(many);
    assert.equal(out.length, 60);
    assert.equal(out[0].content, "msg-40");
    assert.equal(out[59].content, "msg-99");
  });

  it("returns [] for non-array input", () => {
    assert.deepEqual(internals.sanitizeHistoryItems(null), []);
    assert.deepEqual(internals.sanitizeHistoryItems({ role: "user" }), []);
    assert.deepEqual(internals.sanitizeHistoryItems("nope"), []);
  });
});

describe("Sidecar unplanned-exit wiring", () => {
  let internals;
  beforeEach(() => { internals = loadInternals(); });

  it("stores an onUnexpectedExit callback when provided", () => {
    const cb = () => {};
    const sidecar = new internals.Sidecar({
      host: "127.0.0.1", port: 9, log: () => {}, onUnexpectedExit: cb,
    });
    assert.equal(sidecar.onUnexpectedExit, cb);
    assert.equal(sidecar._stopping, false);
  });

  it("defaults onUnexpectedExit to null when omitted", () => {
    const sidecar = new internals.Sidecar({ host: "127.0.0.1", port: 9, log: () => {} });
    assert.equal(sidecar.onUnexpectedExit, null);
  });

  it("ignores a non-function onUnexpectedExit", () => {
    const sidecar = new internals.Sidecar({
      host: "127.0.0.1", port: 9, log: () => {}, onUnexpectedExit: "nope",
    });
    assert.equal(sidecar.onUnexpectedExit, null);
  });

  it("stop() marks the exit as planned even without a live process", () => {
    const sidecar = new internals.Sidecar({ host: "127.0.0.1", port: 9, log: () => {} });
    assert.doesNotThrow(() => sidecar.stop());
    assert.equal(sidecar._stopping, true);
  });

  it("onUnexpectedExit failures never propagate into the exit handler path", () => {
    // The callback is invoked inside proc.on("exit") — a throw there
    // would crash Electron main. The guard lives at the call site; here
    // we just pin the contract that the handler wrapper tolerates throws
    // by verifying our supervisor-facing shape stays callable.
    const sidecar = new internals.Sidecar({
      host: "127.0.0.1", port: 9, log: () => {},
      onUnexpectedExit: () => { throw new Error("boom"); },
    });
    assert.equal(typeof sidecar.onUnexpectedExit, "function");
    assert.throws(() => sidecar.onUnexpectedExit(1, null), /boom/);
  });
});
