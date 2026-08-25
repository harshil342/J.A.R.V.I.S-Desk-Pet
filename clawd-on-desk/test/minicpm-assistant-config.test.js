"use strict";

// Lock down the assistant-prefs → sidecar config contract:
//
//   1. buildAssistantConfigPayload maps the camelCase snapshot onto the
//      exact snake_case body POSTed to /api/config (gateway/runtime_config.py),
//      filling schema defaults for missing/invalid hour keys.
//   2. createAssistantConfigSyncer dedupes against the last SUCCESSFULLY
//      posted payload (lastSentAssistantConfigSig semantics) so repeated
//      controller broadcasts don't spam /api/config — while a failed push
//      is retried on the next trigger.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const targetPath = require.resolve("../src/minicpm-chat.js");

// Re-evaluate the source with a stubbed electron so we get fresh module
// state and can reach otherwise-private helpers via __internals.
function loadInternals() {
  delete require.cache[targetPath];
  const src = fs.readFileSync(targetPath, "utf-8");
  const augmented =
    src +
    "\nmodule.exports.__internals = { ASSISTANT_PREF_DEFAULTS, buildAssistantConfigPayload, createAssistantConfigSyncer };\n";
  const m = new Module(targetPath, module);
  m.filename = targetPath;
  m.paths = Module._nodeModulePaths(path.dirname(targetPath));
  m._compile(augmented, targetPath);
  return m.exports.__internals;
}

// Stub electron BEFORE the file is loaded (same approach as minicpm-locate).
const realResolve = Module._resolveFilename;
const fakeElectronPath = path.join(os.tmpdir(), "fake-electron-stub-assistant-config.js");
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

describe("buildAssistantConfigPayload", () => {
  const { ASSISTANT_PREF_DEFAULTS, buildAssistantConfigPayload } = loadInternals();

  it("maps a full snapshot onto the exact snake_case wire body", () => {
    const payload = buildAssistantConfigPayload({
      assistantAddress: "boss",
      clarifyStrength: "confirm_all",
      autoMemory: false,
      briefingHour: 6,
      recapHour: 22,
    });
    assert.deepStrictEqual(payload, {
      assistant_address: "boss",
      clarify_strength: "confirm_all",
      auto_memory: false,
      briefing_hour: 6,
      recap_hour: 22,
    });
    // Exact serialized shape guards against stray/renamed keys reaching
    // the gateway (unknown keys are ignored there, but silence hides bugs).
    assert.equal(
      JSON.stringify(payload),
      '{"assistant_address":"boss","clarify_strength":"confirm_all","auto_memory":false,"briefing_hour":6,"recap_hour":22}'
    );
  });

  it("fills schema defaults when keys are absent", () => {
    const payload = buildAssistantConfigPayload({});
    assert.deepStrictEqual(payload, {
      assistant_address: undefined,
      clarify_strength: undefined,
      auto_memory: false,
      briefing_hour: ASSISTANT_PREF_DEFAULTS.briefingHour,
      recap_hour: ASSISTANT_PREF_DEFAULTS.recapHour,
    });
    // Real callers pass the projected snapshot, which overlays every
    // default — that full-defaults body must serialize cleanly.
    const projected = buildAssistantConfigPayload({ ...ASSISTANT_PREF_DEFAULTS });
    assert.deepEqual(
      JSON.stringify(projected),
      '{"assistant_address":"sir","clarify_strength":"ambiguous","auto_memory":true,"briefing_hour":8,"recap_hour":21}'
    );
  });

  it("falls back to schema defaults for non-integer hours", () => {
    const payload = buildAssistantConfigPayload({
      briefingHour: "7",
      recapHour: null,
    });
    assert.equal(payload.briefing_hour, ASSISTANT_PREF_DEFAULTS.briefingHour);
    assert.equal(payload.recap_hour, ASSISTANT_PREF_DEFAULTS.recapHour);

    const fractional = buildAssistantConfigPayload({
      briefingHour: 7.5,
      recapHour: NaN,
    });
    assert.equal(fractional.briefing_hour, ASSISTANT_PREF_DEFAULTS.briefingHour);
    assert.equal(fractional.recap_hour, ASSISTANT_PREF_DEFAULTS.recapHour);
  });

  it("coerces auto_memory to boolean and tolerates null snapshots", () => {
    assert.equal(buildAssistantConfigPayload({ autoMemory: 1 }).auto_memory, true);
    assert.equal(buildAssistantConfigPayload({ autoMemory: "" }).auto_memory, false);
    assert.doesNotThrow(() => buildAssistantConfigPayload(null));
  });
});

describe("createAssistantConfigSyncer dedupe", () => {
  const { createAssistantConfigSyncer } = loadInternals();

  const SNAP_A = {
    assistantAddress: "sir",
    clarifyStrength: "ambiguous",
    autoMemory: true,
    briefingHour: 8,
    recapHour: 21,
  };
  const SNAP_B = { ...SNAP_A, recapHour: 22 };

  it("posts once per distinct payload and dedupes identical repeats", async () => {
    const calls = [];
    const send = createAssistantConfigSyncer(async (payload) => {
      calls.push(payload);
    });

    assert.equal(await send(SNAP_A), true);
    assert.equal(await send(SNAP_A), false); // deduped — no second POST
    assert.equal(calls.length, 1);

    assert.equal(await send(SNAP_B), true); // changed pref → posts again
    assert.equal(await send(SNAP_B), false);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].recap_hour, 22);
  });

  it("retries after a failed post (signature marked only on success)", async () => {
    let shouldFail = true;
    let attempts = 0;
    const send = createAssistantConfigSyncer(async () => {
      attempts += 1;
      if (shouldFail) throw new Error("sidecar down");
    });

    await assert.rejects(() => send(SNAP_A), /sidecar down/);
    assert.equal(attempts, 1);

    // Same snapshot again MUST retry — the failed push was not recorded.
    shouldFail = false;
    assert.equal(await send(SNAP_A), true);
    assert.equal(attempts, 2);

    // Only now does the identical snapshot dedupe.
    assert.equal(await send(SNAP_A), false);
    assert.equal(attempts, 2);
  });

  it("treats equal-content snapshots as identical regardless of identity", async () => {
    let posts = 0;
    const send = createAssistantConfigSyncer(async () => {
      posts += 1;
    });
    await send({ ...SNAP_A });
    await send({ ...SNAP_A }); // fresh object, same content → deduped
    assert.equal(posts, 1);
  });
});
