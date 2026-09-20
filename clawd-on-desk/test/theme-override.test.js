// test/theme-override.test.js — Path A / Phase 3b theme overrides
//
// Covers three layers:
//   1. state.js applyState() gate: when ctx.isOneshotDisabled(state)
//      marks a oneshot state disabled, both visual + sound are skipped,
//      falling back to resolveDisplayState
//   2. settings-actions setThemeOverrideDisabled / resetThemeOverrides whitelist
//      validation + commit computation
//   3. settings-actions setAnimationOverride file / transition / autoReturn writes

"use strict";

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert");
const path = require("path");

const themeLoader = require("../src/theme-loader");
themeLoader.init(path.join(__dirname, "..", "src"));
const _defaultTheme = themeLoader.loadTheme("cloudling");

const {
  commandRegistry,
  ONESHOT_OVERRIDE_STATES,
} = require("../src/settings-actions");
const prefs = require("../src/prefs");

// ── state.js gate tests ────────────────────────────────────────────────────

function makeCtx(overrides = {}) {
  const stateChanges = [];
  const sounds = [];
  const ctx = {
    theme: _defaultTheme,
    doNotDisturb: false,
    miniTransitioning: false,
    miniMode: false,
    mouseOverPet: false,
    idlePaused: false,
    forceEyeResend: false,
    eyePauseUntil: 0,
    mouseStillSince: Date.now(),
    miniSleepPeeked: false,
    playSound: (name) => sounds.push(name),
    sendToRenderer: (ch, ...args) => {
      if (ch === "state-change") stateChanges.push(args);
    },
    syncHitWin: () => {},
    sendToHitWin: () => {},
    miniPeekIn: () => {},
    miniPeekOut: () => {},
    buildContextMenu: () => {},
    buildTrayMenu: () => {},
    pendingPermissions: [],
    resolvePermissionEntry: () => {},
    t: (k) => k,
    focusTerminalWindow: () => {},
    processKill: () => { const e = new Error("ESRCH"); e.code = "ESRCH"; throw e; },
    getCursorScreenPoint: () => ({ x: 100, y: 100 }),
    isOneshotDisabled: () => false,
    ...overrides,
  };
  ctx._stateChanges = stateChanges;
  ctx._sounds = sounds;
  return ctx;
}

// Helper returning true only when the disabled set contains that state
function disabledSet(set) {
  return (stateKey) => set.has(stateKey);
}

describe("state.js applyState() gate", () => {
  let api;
  let ctx;
  afterEach(() => { if (api) api.cleanup(); api = null; });

  it("non-disabled oneshot: attention plays normally", () => {
    ctx = makeCtx({ isOneshotDisabled: () => false });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    ctx._sounds.length = 0;
    api.applyState("attention");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(played.includes("attention"), "attention should play");
    assert.ok(ctx._sounds.includes("complete"), "attention should play complete sound");
  });

  it("disabled attention: no visual, no sound, falls back to idle", () => {
    ctx = makeCtx({ isOneshotDisabled: disabledSet(new Set(["attention"])) });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    ctx._sounds.length = 0;
    api.applyState("attention");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(!played.includes("attention"), "should not play attention");
    assert.ok(!ctx._sounds.includes("complete"), "should not play complete");
  });

  it("disabled notification: no mini-alert in mini-mode either (gate runs before mini mapping)", () => {
    ctx = makeCtx({
      miniMode: true,
      isOneshotDisabled: disabledSet(new Set(["notification"])),
    });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    ctx._sounds.length = 0;
    api.applyState("notification");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(!played.includes("notification"), "should not play notification");
    assert.ok(!played.includes("mini-alert"), "should not play mini-alert either");
    assert.ok(!ctx._sounds.includes("confirm"), "should not play confirm");
  });

  it("disabled attention: no mini-happy mapping in mini-mode", () => {
    ctx = makeCtx({
      miniMode: true,
      isOneshotDisabled: disabledSet(new Set(["attention"])),
    });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    api.applyState("attention");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(!played.includes("mini-happy"), "mini-happy should not appear");
  });

  it("PermissionRequest path (updateSession) blocked by gate", () => {
    ctx = makeCtx({ isOneshotDisabled: disabledSet(new Set(["notification"])) });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    ctx._sounds.length = 0;
    api.updateSession("s1", "any", "PermissionRequest", {
      cwd: "/tmp",
      agentId: "claude-code",
    });
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(!played.includes("notification"), "PermissionRequest path should not play notification");
    assert.ok(!ctx._sounds.includes("confirm"), "should not play confirm");
  });

  it("non-oneshot state unaffected by gate (even when ctx.isOneshotDisabled wrongly returns true)", () => {
    // Simulate a buggy ctx: returns true for every state
    ctx = makeCtx({ isOneshotDisabled: () => true });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    api.applyState("working");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(played.includes("working"), "working is not oneshot, gate should not consume it");
  });

  it("all 5 oneshots disabled: applyState(attention) falls back to idle (no session)", () => {
    ctx = makeCtx({
      isOneshotDisabled: disabledSet(new Set([
        "attention", "error", "sweeping", "notification", "carrying",
      ])),
    });
    api = require("../src/state")(ctx);
    ctx._stateChanges.length = 0;
    api.applyState("attention");
    const played = ctx._stateChanges.map((a) => a[0]);
    // gate falls back to resolveDisplayState() -> empty sessions -> "idle"
    // initial state is itself idle, so sameState may emit 0 times; absence of attention is what matters
    assert.ok(!played.includes("attention"));
    assert.ok(!played.includes("error"));
    assert.ok(!played.includes("sweeping"));
    assert.ok(!played.includes("notification"));
    assert.ok(!played.includes("carrying"));
  });

  it("attention disabled + working session: falls back to working, not attention", () => {
    ctx = makeCtx({ isOneshotDisabled: disabledSet(new Set(["attention"])) });
    api = require("../src/state")(ctx);
    // Trigger a working session first so resolveDisplayState returns working
    api.updateSession("s1", "working", "PreToolUse", {
      cwd: "/tmp",
      agentId: "claude-code",
    });
    ctx._stateChanges.length = 0;
    ctx._sounds.length = 0;
    api.applyState("attention");
    const played = ctx._stateChanges.map((a) => a[0]);
    assert.ok(!played.includes("attention"));
    // Fallback target is working, or sameState no-emit; both are valid
    if (played.length > 0) {
      assert.strictEqual(played[0], "working");
    }
  });
});

// ── settings-actions: setThemeOverrideDisabled / resetThemeOverrides ──────

describe("setThemeOverrideDisabled", () => {
  const action = commandRegistry.setThemeOverrideDisabled;
  const baseSnap = () => ({ ...prefs.getDefaults(), themeOverrides: {} });

  it("enable (disabled:true) first write produces {disabled:true}", () => {
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: true },
      { snapshot: baseSnap() },
    );
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: { states: { attention: { disabled: true } } },
    });
  });

  it("same-value noop produces no commit", () => {
    const snap = baseSnap();
    snap.themeOverrides = { clawd: { states: { attention: { disabled: true } } } };
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: true },
      { snapshot: snap },
    );
    assert.strictEqual(r.status, "ok");
    assert.strictEqual(r.noop, true);
    assert.ok(!r.commit);
  });

  it("disabled:false cleans up key and removes theme entry when theme map goes empty", () => {
    const snap = baseSnap();
    snap.themeOverrides = { clawd: { states: { attention: { disabled: true } } } };
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: false },
      { snapshot: snap },
    );
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(r.commit.themeOverrides, {});
  });

  it("disabled:false keeps other disabled states", () => {
    const snap = baseSnap();
    snap.themeOverrides = {
      clawd: {
        states: {
          attention: { disabled: true },
          sweeping:  { disabled: true },
        },
      },
    };
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: false },
      { snapshot: snap },
    );
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: { states: { sweeping: { disabled: true } } },
    });
  });

  it("disabled:false preserves file field on file-form entries (forward-compat)", () => {
    const snap = baseSnap();
    snap.themeOverrides = {
      clawd: {
        states: {
          attention: { disabled: true, sourceThemeId: "clawd", file: "clawd-happy.svg" },
        },
      },
    };
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: false },
      { snapshot: snap },
    );
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: { states: { attention: { sourceThemeId: "clawd", file: "clawd-happy.svg" } } },
    });
  });

  it("theme isolation: disabling in themeA does not affect themeB", () => {
    const snap = baseSnap();
    snap.themeOverrides = { calico: { states: { attention: { disabled: true } } } };
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: true },
      { snapshot: snap },
    );
    assert.deepStrictEqual(r.commit.themeOverrides, {
      calico: { states: { attention: { disabled: true } } },
      clawd:  { states: { attention: { disabled: true } } },
    });
  });

  it("non-whitelisted stateKey is rejected", () => {
    for (const badKey of ["idle", "working", "juggling", "thinking", "sleeping", "waking"]) {
      const r = action(
        { themeId: "clawd", stateKey: badKey, disabled: true },
        { snapshot: baseSnap() },
      );
      assert.strictEqual(r.status, "error", `${badKey} should be rejected`);
    }
  });

  it("all whitelisted stateKeys are accepted", () => {
    for (const key of ONESHOT_OVERRIDE_STATES) {
      const r = action(
        { themeId: "clawd", stateKey: key, disabled: true },
        { snapshot: baseSnap() },
      );
      assert.strictEqual(r.status, "ok", `${key} should be accepted`);
    }
  });

  it("disabled must be boolean", () => {
    const r = action(
      { themeId: "clawd", stateKey: "attention", disabled: "yes" },
      { snapshot: baseSnap() },
    );
    assert.strictEqual(r.status, "error");
  });

  it("themeId must be a non-empty string", () => {
    const r1 = action(
      { themeId: "", stateKey: "attention", disabled: true },
      { snapshot: baseSnap() },
    );
    const r2 = action(
      { themeId: null, stateKey: "attention", disabled: true },
      { snapshot: baseSnap() },
    );
    assert.strictEqual(r1.status, "error");
    assert.strictEqual(r2.status, "error");
  });

  it("non-object payload errors", () => {
    assert.strictEqual(action(null, { snapshot: baseSnap() }).status, "error");
    assert.strictEqual(action("clawd", { snapshot: baseSnap() }).status, "error");
  });
});

describe("resetThemeOverrides", () => {
  const action = commandRegistry.resetThemeOverrides;
  const baseSnap = () => ({ ...prefs.getDefaults(), themeOverrides: {} });

  it("clears all overrides of the current theme", () => {
    const snap = baseSnap();
    snap.theme = "calico";
    snap.themeOverrides = {
      clawd: {
        states: {
          attention: { disabled: true },
          notification: { disabled: true },
        },
      },
      calico: { states: { error: { disabled: true } } },
    };
    const r = action({ themeId: "clawd" }, { snapshot: snap });
    assert.strictEqual(r.status, "ok");
    // clawd cleared entirely, calico kept
    assert.deepStrictEqual(r.commit.themeOverrides, {
      calico: { states: { error: { disabled: true } } },
    });
  });

  it("noop when the theme has no overrides", () => {
    const r = action({ themeId: "clawd" }, { snapshot: baseSnap() });
    assert.strictEqual(r.status, "ok");
    assert.strictEqual(r.noop, true);
    assert.ok(!r.commit);
  });

  it("accepts string payload shorthand", () => {
    const snap = baseSnap();
    snap.theme = "calico";
    snap.themeOverrides = { clawd: { states: { attention: { disabled: true } } } };
    const r = action("clawd", { snapshot: snap });
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(r.commit.themeOverrides, {});
  });

  it("empty themeId errors", () => {
    const r = action({ themeId: "" }, { snapshot: baseSnap() });
    assert.strictEqual(r.status, "error");
  });

  it("reset on current theme explicitly reloads runtime theme (overrideMap=null)", () => {
    const snap = baseSnap();
    snap.theme = "clawd";
    snap.themeOverrides = {
      clawd: { states: { attention: { disabled: true } } },
    };
    const calls = [];
    const r = action(
      { themeId: "clawd" },
      {
        snapshot: snap,
        activateTheme: (themeId, variantId, overrideMap) => {
          calls.push({ themeId, variantId, overrideMap });
        },
      },
    );
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(calls, [{
      themeId: "clawd",
      variantId: null,
      overrideMap: null,
    }]);
    assert.deepStrictEqual(r.commit.themeOverrides, {});
  });

  it("reset on current theme without activateTheme dep returns error", () => {
    const snap = baseSnap();
    snap.theme = "clawd";
    snap.themeOverrides = {
      clawd: { states: { attention: { disabled: true } } },
    };
    const r = action({ themeId: "clawd" }, { snapshot: snap });
    assert.strictEqual(r.status, "error");
    assert.match(r.message, /activateTheme/);
  });
});

describe("setAnimationOverride", () => {
  const action = commandRegistry.setAnimationOverride;
  const baseSnap = () => ({ ...prefs.getDefaults(), theme: "clawd", themeOverrides: {} });

  it("writes state file + transition + autoReturn into nested schema", () => {
    const calls = [];
    const r = action(
      {
        themeId: "clawd",
        slotType: "state",
        stateKey: "attention",
        file: "custom-attention.svg",
        transition: { in: 120, out: 180 },
        autoReturnMs: 2600,
      },
      {
        snapshot: baseSnap(),
        activateTheme: (themeId, variantId, overrides) => {
          calls.push({ themeId, variantId, overrides });
          return { themeId, variantId: "default" };
        },
      },
    );
    assert.strictEqual(r.status, "ok");
    assert.strictEqual(calls.length, 1);
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: {
        states: {
          attention: {
            file: "custom-attention.svg",
            transition: { in: 120, out: 180 },
          },
        },
        timings: {
          autoReturn: { attention: 2600 },
        },
      },
    });
  });

  it("writes tier file + transition, keyed by originalFile", () => {
    const r = action(
      {
        themeId: "clawd",
        slotType: "tier",
        tierGroup: "workingTiers",
        originalFile: "clawd-working-typing.svg",
        file: "custom-working.svg",
        transition: { in: 0, out: 90 },
      },
      {
        snapshot: baseSnap(),
        activateTheme: () => ({ themeId: "clawd", variantId: "default" }),
      },
    );
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: {
        tiers: {
          workingTiers: {
            "clawd-working-typing.svg": {
              file: "custom-working.svg",
              transition: { in: 0, out: 90 },
            },
          },
        },
      },
    });
  });

  it("writes idleAnimation file + transition + duration, keyed by originalFile", () => {
    const r = action(
      {
        themeId: "clawd",
        slotType: "idleAnimation",
        originalFile: "idle-look.svg",
        file: "custom-idle-look.svg",
        transition: { in: 40, out: 110 },
        durationMs: 4200,
      },
      {
        snapshot: baseSnap(),
        activateTheme: () => ({ themeId: "clawd", variantId: "default" }),
      },
    );
    assert.strictEqual(r.status, "ok");
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: {
        idleAnimations: {
          "idle-look.svg": {
            file: "custom-idle-look.svg",
            transition: { in: 40, out: 110 },
            durationMs: 4200,
          },
        },
      },
    });
  });

  it("non-current theme skips activateTheme but still commits override", () => {
    const calls = [];
    const snap = baseSnap();
    snap.theme = "calico";
    const r = action(
      {
        themeId: "clawd",
        slotType: "state",
        stateKey: "error",
        file: "x.svg",
      },
      {
        snapshot: snap,
        activateTheme: (...args) => { calls.push(args); },
      },
    );
    assert.strictEqual(r.status, "ok");
    assert.strictEqual(calls.length, 0);
    assert.deepStrictEqual(r.commit.themeOverrides, {
      clawd: { states: { error: { file: "x.svg" } } },
    });
  });

  it("current theme without activateTheme dep returns error", () => {
    const r = action(
      {
        themeId: "clawd",
        slotType: "state",
        stateKey: "attention",
        file: "x.svg",
      },
      { snapshot: baseSnap() },
    );
    assert.strictEqual(r.status, "error");
    assert.match(r.message, /activateTheme/);
  });
});
