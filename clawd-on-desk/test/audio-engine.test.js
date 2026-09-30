"use strict";

const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  AudioEngine,
  loadVoiceManifest,
  pickClip,
  priorityOf,
  clampVolume,
  isEvent,
  MAX_CONCURRENT,
  CUE_COOLDOWN_MS,
} = require("../src/audio-engine.js");

function themeWith(manifest) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "deskpet-voice-"));
  fs.writeFileSync(path.join(dir, "voice.json"), JSON.stringify(manifest), "utf8");
  return dir;
}

const GOOD = {
  id: "test",
  voiceprint: { ref: "r.wav", provenance: { source: "synthetic", license: "CC0", verified: "2026-10-01" } },
  defaults: { volume: 0.12, maxVolume: 0.2 },
  clips: { approval: "approval", error: "error", working: "work", finished: "finish" },
  variants: { working: ["work_a", "work_b", "work_c"] },
};

describe("priority", () => {
  it("orders the events that must not be reordered", () => {
    assert.ok(priorityOf("approval") < priorityOf("error"));
    assert.ok(priorityOf("error") < priorityOf("finished"));
    assert.ok(priorityOf("finished") < priorityOf("working"));
    assert.ok(priorityOf("working") < priorityOf("sleeping"));
  });

  it("treats an unknown event as lowest, never as high", () => {
    assert.equal(isEvent("approval"), true);
    assert.equal(isEvent("definitely-not-an-event"), false);
    assert.equal(priorityOf("definitely-not-an-event"), Number.MAX_SAFE_INTEGER);
  });
});

describe("volume is clamped to the pet's range", () => {
  it("defaults to 0.12 and caps at 0.2", () => {
    assert.equal(clampVolume(undefined), 0.12);
    assert.equal(clampVolume(1), 0.2);
    assert.equal(clampVolume(0.5), 0.2);
    assert.equal(clampVolume(-1), 0);
    assert.equal(clampVolume("nonsense"), 0.12);
  });
});

describe("voiceprint provenance is required", () => {
  it("refuses a manifest whose provenance is incomplete", () => {
    const dir = themeWith({ ...GOOD, voiceprint: { ref: "r.wav", provenance: { source: "", license: "" } } });
    const m = loadVoiceManifest(dir);
    assert.equal(m.ok, false);
    assert.match(m.reason, /provenance/);
  });

  it("accepts a fully attributed voiceprint", () => {
    assert.equal(loadVoiceManifest(themeWith(GOOD)).ok, true);
  });

  it("accepts no voiceprint at all (clips only)", () => {
    const { voiceprint, ...clipsOnly } = GOOD;
    assert.equal(loadVoiceManifest(themeWith(clipsOnly)).ok, true);
  });

  it("refuses clips for events that do not exist", () => {
    const m = loadVoiceManifest(themeWith({ ...GOOD, clips: { ...GOOD.clips, teleport: "x" } }));
    assert.equal(m.ok, false);
    assert.match(m.reason, /teleport/);
  });

  it("stays quiet when there is no manifest at all", () => {
    const engine = new AudioEngine();
    assert.equal(engine.load(path.join(os.tmpdir(), "deskpet-does-not-exist-xyz")), false);
    assert.equal(engine.resolve("working").play, false);
  });
});

describe("variants never repeat back to back", () => {
  it("picks a different variant when alternatives exist", () => {
    // Seeded: an unseeded Math.random loop is a flaky test, not a test.
    const draws = [0, 0.9, 0.45];
    let n = 0;
    const rand = () => draws[n++ % draws.length];
    const last = {};
    const seen = new Set();
    for (let i = 0; i < 9; i++) {
      const previous = last.working;
      const pick = pickClip({ clips: GOOD.clips, variants: GOOD.variants }, "working", last, rand);
      assert.notEqual(pick, previous, `repeated ${pick} immediately after itself`);
      seen.add(pick);
      last.working = pick;
    }
    assert.deepEqual([...seen].sort(), ["work_a", "work_b", "work_c"]);
  });

  it("repeats the only clip when there is one", () => {
    const m = { clips: { error: "error" }, variants: {} };
    assert.equal(pickClip(m, "error", { error: "error" }), "error");
  });
});

describe("resolution", () => {
  let t = 0;
  let engine;

  beforeEach(() => {
    t = 1_000_000;
    engine = new AudioEngine({ now: () => t, rand: () => 0 });
    engine.load(themeWith(GOOD));
  });

  it("plays a cue and returns its clip and volume", () => {
    const r = engine.resolve("working");
    assert.equal(r.play, true);
    assert.equal(r.tier, "cue");
    assert.equal(r.volume, 0.12);
    assert.ok(["work_a", "work_b", "work_c"].includes(r.clip));
  });

  it("honours mute and do-not-disturb", () => {
    assert.equal(engine.resolve("working", { muted: true }).reason, "muted");
    assert.equal(engine.resolve("working", { dnd: true }).reason, "dnd");
  });

  it("cooldowns per event, not globally", () => {
    // The bug this replaces: one 10s cooldown meant a permission request
    // arriving while a "working" cue was live was swallowed entirely.
    assert.equal(engine.resolve("working").play, true);
    const approval = engine.resolve("approval");
    assert.equal(approval.play, true, "an approval must not be silenced by an unrelated cue");
    assert.equal(approval.clip, "approval");
  });

  it("still cools down the same event", () => {
    assert.equal(engine.resolve("working").play, true);
    t += 1000;
    assert.equal(engine.resolve("working").reason, "cooldown");
    t += CUE_COOLDOWN_MS;
    assert.equal(engine.resolve("working").play, true);
  });

  it("lets a higher priority preempt a lower one that is playing", () => {
    const r = engine.resolve("approval", { active: new Set(["working"]) });
    assert.equal(r.play, true);
    assert.deepEqual(r.preempt, undefined);
  });

  it("suppresses a lower priority while something louder is active", () => {
    const r = engine.resolve("working", { active: new Set(["approval"]) });
    assert.equal(r.play, false);
    assert.match(r.reason, /preempted by approval/);
  });

  it("suppresses a same-priority event past the concurrency cap", () => {
    const active = new Set(["working", "thinking"]);
    assert.ok(active.size >= MAX_CONCURRENT);
    const r = engine.resolve("sleeping", { active });
    assert.equal(r.play, false);
  });

  it("gives a spoken line a bounded budget and a longer cooldown", () => {
    const r = engine.resolve("finished", { tier: "line" });
    assert.equal(r.play, true);
    assert.ok(r.budgetMs > 0 && r.budgetMs <= 2500, "a line must not talk over the next event");
    assert.equal(engine.resolve("finished", { tier: "line" }).reason, "cooldown");
  });

  it("has nothing to say for an event the theme does not cover", () => {
    assert.equal(engine.resolve("sleeping").reason, "no clip for sleeping");
  });
});

describe("the app default manifest is valid and licence-clean", () => {
  const DEFAULT_MANIFEST = path.join(__dirname, "..", "assets", "voice.default.json");

  it("loads", () => {
    const m = loadVoiceManifest(null, path.join(__dirname, "..", "assets"));
    assert.equal(m.ok, true, m.reason);
    assert.ok(m.clips.approval, "an approval cue is the one that must always exist");
  });

  it("declares no voiceprint, so it carries no third-party audio", () => {
    const m = loadVoiceManifest(null, path.join(__dirname, "..", "assets"));
    assert.equal(m.voiceprint, null);
  });

  it("only references clips that exist on disk", () => {
    const sounds = path.join(__dirname, "..", "assets", "sounds");
    const m = loadVoiceManifest(null, path.join(__dirname, "..", "assets"));
    const ids = new Set();
    for (const id of Object.values(m.clips)) ids.add(id);
    for (const list of Object.values(m.variants)) for (const id of list) ids.add(id);
    for (const id of ids) {
      assert.ok(fs.existsSync(path.join(sounds, `${id}.wav`)), `manifest references a missing clip: ${id}`);
    }
  });

  it("keeps every cue inside the 400 ms budget", () => {
    const sounds = path.join(__dirname, "..", "assets", "sounds");
    const m = loadVoiceManifest(null, path.join(__dirname, "..", "assets"));
    for (const id of new Set(Object.values(m.clips))) {
      const file = path.join(sounds, `${id}.wav`);
      if (!fs.existsSync(file)) continue;
      const bytes = fs.statSync(file).size;
      const seconds = (bytes - 44) / 2 / 44100;
      assert.ok(seconds <= 0.4, `${id} is ${Math.round(seconds * 1000)}ms, over the cue budget`);
    }
  });

  it("is the fallback when a theme has no voice.json of its own", () => {
    const theme = fs.mkdtempSync(path.join(os.tmpdir(), "deskpet-bare-"));
    const m = loadVoiceManifest(theme, path.join(__dirname, "..", "assets"));
    assert.equal(m.ok, true);
    assert.equal(path.basename(m.source), "voice.default.json");
  });

  it("is overridden when a theme ships its own", () => {
    const theme = themeWith({ ...GOOD, clips: { approval: "custom_approval" } });
    const m = loadVoiceManifest(theme, path.join(__dirname, "..", "assets"));
    assert.equal(m.clips.approval, "custom_approval");
    assert.equal(path.basename(m.source), "voice.json");
  });
});

describe("ducking lowers the bed instead of cutting it", () => {
  it("ramps down and clamps at zero", () => {
    const engine = new AudioEngine();
    assert.equal(engine.duck(0.5), 0.5);
    assert.equal(engine.duck(0.9), 1);
    assert.equal(engine.duck(-5), 0);
  });
});

describe("preload list", () => {
  it("covers every clip and variant exactly once", () => {
    const engine = new AudioEngine();
    engine.load(themeWith(GOOD));
    const urls = engine.preloadUrls((id) => `/audio/${id}.wav`);
    assert.equal(new Set(urls).size, urls.length, "no duplicate preloads");
    for (const id of ["approval", "error", "finish", "work_a", "work_b", "work_c"]) {
      assert.ok(urls.includes(`/audio/${id}.wav`), `missing ${id}`);
    }
  });

  it("is empty when the theme has no voice", () => {
    const engine = new AudioEngine();
    assert.deepEqual(engine.preloadUrls((id) => id), []);
  });
});
