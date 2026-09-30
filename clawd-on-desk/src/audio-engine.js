"use strict";

// Audio engine: decides what the pet should make a noise about, and how loudly.
//
// The previous design was one global 10-second cooldown in main.js, so the
// first sound to fire silenced every other sound for ten seconds — including a
// permission request arriving while a "working" cue was still relevant. This
// replaces that with per-event priority, a concurrency cap, and ducking.
//
// Two tiers, deliberately different (plan.md D5/D6):
//   cue  a state change. Sound only, short.
//   line a moment worth speaking. Sound plus one spoken line, cancellable.
//
// "Unprompted through the day" is not a tier. Silence is the default state.
//
// No Electron imports: resolution is pure so it is unit-testable without a
// window, and the renderer stays responsible for decoding and playback.

const fs = require("node:fs");
const path = require("node:path");

// Lower number wins. An event only plays if nothing more important is already
// speaking; equal priority is allowed up to the concurrency cap.
const PRIORITY = {
  approval: 0,
  error: 1,
  attention: 2,
  finished: 3,
  wake: 3,
  line: 4,
  working: 5,
  thinking: 6,
  sleeping: 7,
  idle: 8,
};

const MAX_CONCURRENT = 2;
// Matches Coucou's SoundEngine: 0.12 default against a 0.2 ceiling. Full-scale
// clips at 1.0 are startling for a thing that lives in the corner of a screen.
const DEFAULT_VOLUME = 0.12;
const MAX_VOLUME = 0.2;
// Cooldown is per event, not global. A cue repeated every 400ms is a machine
// gun; the same cue every 8s is a heartbeat.
const CUE_COOLDOWN_MS = 8000;
const LINE_COOLDOWN_MS = 15000;
const LINE_BUDGET_MS = 2500;

const VALID_EVENTS = new Set(Object.keys(PRIORITY));

function isEvent(name) {
  return typeof name === "string" && VALID_EVENTS.has(name);
}

function priorityOf(name) {
  return isEvent(name) ? PRIORITY[name] : Number.MAX_SAFE_INTEGER;
}

function clampVolume(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return DEFAULT_VOLUME;
  return Math.max(0, Math.min(MAX_VOLUME, n));
}

/**
 * Read and validate a theme's voice.json.
 *
 * `voiceprint.provenance` is mandatory and must be non-empty. An empty value
 * means nobody has recorded where the audio came from, and shipping that is how
 * a licence question turns into a takedown. Fail closed: the caller goes quiet
 * rather than playing unattributable audio.
 */
function loadVoiceManifest(themeDir, fallbackDir) {
  // A theme's own voice.json wins; otherwise the app default. One shared default
  // rather than a copy per theme, so a cue change lands everywhere at once and a
  // theme still overrides simply by shipping its own file.
  const candidates = themeDir ? [path.join(themeDir, "voice.json")] : [];
  if (fallbackDir) candidates.push(path.join(fallbackDir, "voice.default.json"));

  let raw = null;
  let source = null;
  let reason = "no voice manifest";
  for (const file of candidates) {
    let parsed;
    try {
      parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (err) {
      if (err && err.code === "ENOENT") continue;
      reason = `${path.basename(file)} is not readable JSON`;
      continue;
    }
    raw = parsed;
    source = file;
    break;
  }
  if (raw === null) return { ok: false, reason };

  if (!raw || typeof raw !== "object") return { ok: false, reason: "voice manifest is not an object" };

  const vp = raw.voiceprint;
  if (vp && (!vp.provenance || !vp.provenance.source || !vp.provenance.license)) {
    return { ok: false, reason: "voiceprint.provenance.source and .license are both required" };
  }

  const clips = raw.clips && typeof raw.clips === "object" ? raw.clips : {};
  const variants = raw.variants && typeof raw.variants === "object" ? raw.variants : {};
  const bad = Object.keys(clips).filter((k) => !isEvent(k));
  if (bad.length) return { ok: false, reason: `clips reference unknown events: ${bad.join(", ")}` };

  return {
    ok: true,
    source,
    id: typeof raw.id === "string" ? raw.id : null,
    voiceprint: vp || null,
    clips,
    variants,
    volume: clampVolume(raw.defaults && raw.defaults.volume),
    maxVolume: clampVolume(raw.defaults && raw.defaults.maxVolume) || MAX_VOLUME,
  };
}

/**
 * Pick a clip, avoiding an immediate repeat when variants exist.
 * `lastPlayed` maps event -> the clip id that fired most recently.
 */
function pickClip(manifest, event, lastPlayed, rand) {
  const base = manifest.clips[event];
  if (!base) return null;
  const pool = Array.isArray(manifest.variants[event]) && manifest.variants[event].length
    ? manifest.variants[event]
    : [base];
  // Filter the previous pick out of a multi-entry pool. An earlier version
  // short-circuited on `pool[0] === previous`, which fired whenever the last
  // pick happened to be the pool's first element and returned it again — the
  // pet repeated the same clip forever.
  const options = pool.length > 1 ? pool.filter((c) => c !== lastPlayed[event]) : pool;
  if (!options.length) return pool[0];
  const r = (rand || Math.random)();
  return options[Math.floor(r * options.length) % options.length];
}

class AudioEngine {
  constructor({ now = () => Date.now(), rand = Math.random } = {}) {
    this.now = now;
    this.rand = rand;
    this.manifest = { ok: false, reason: "no manifest loaded", clips: {}, variants: {} };
    this.lastPlayed = {};
    this.lastFiredAt = {};
    this.ducked = 0;
  }

  load(themeDir, fallbackDir) {
    this.manifest = loadVoiceManifest(themeDir, fallbackDir);
    this.lastPlayed = {};
    this.lastFiredAt = {};
    if (!this.manifest.ok) this.note(`audio: ${this.manifest.reason}`);
    return this.manifest.ok;
  }

  note(message) {
    if (typeof this.onLog === "function") this.onLog(message);
  }

  get enabled() {
    return Boolean(this.manifest.ok);
  }

  /** Duck the bed rather than cutting it — silence reads as a crash. */
  duck(amount) {
    this.ducked = Math.max(0, Math.min(1, this.ducked + amount));
    return this.ducked;
  }

  /**
   * Resolve whether `event` should sound right now, and what should play.
   * Returns `{ play: false, reason }` when suppressed, so callers can log it.
   *
   * `active` is the set of events currently audible.
   */
  resolve(event, { muted = false, dnd = false, active = new Set(), tier = "cue" } = {}) {
    if (muted) return { play: false, reason: "muted" };
    if (dnd) return { play: false, reason: "dnd" };
    if (!isEvent(event)) return { play: false, reason: `unknown event: ${event}` };
    if (!this.enabled) return { play: false, reason: this.manifest.reason };

    const p = priorityOf(event);
    const louder = [...active].filter((e) => priorityOf(e) < p);
    if (louder.length) {
      return { play: false, reason: `preempted by ${louder.join(", ")}`, preempt: louder };
    }
    const samePriority = [...active].filter((e) => priorityOf(e) === p);
    if (active.size + samePriority.length > MAX_CONCURRENT && samePriority.length) {
      return { play: false, reason: "concurrency cap" };
    }

    const cooldown = tier === "line" ? LINE_COOLDOWN_MS : CUE_COOLDOWN_MS;
    const last = this.lastFiredAt[event] || 0;
    if (this.now() - last < cooldown) {
      return { play: false, reason: "cooldown" };
    }

    const clip = pickClip(this.manifest, event, this.lastPlayed, this.rand);
    if (!clip) return { play: false, reason: `no clip for ${event}` };

    this.lastPlayed[event] = clip;
    this.lastFiredAt[event] = this.now();

    return {
      play: true,
      event,
      clip,
      tier,
      priority: p,
      volume: this.manifest.volume,
      // A spoken line is bounded so a long reply cannot leave the pet talking
      // over the next event; the next event simply cuts it.
      budgetMs: tier === "line" ? LINE_BUDGET_MS : null,
      duckTo: this.manifest.volume,
    };
  }

  /** Ids the renderer should preload so the first cue has no latency. */
  preloadUrls(resolveUrl) {
    if (!this.enabled || typeof resolveUrl !== "function") return [];
    const ids = new Set();
    for (const event of Object.keys(this.manifest.clips)) {
      ids.add(this.manifest.clips[event]);
      for (const v of this.manifest.variants[event] || []) ids.add(v);
    }
    return [...ids].map((id) => resolveUrl(id)).filter(Boolean);
  }
}

module.exports = {
  AudioEngine,
  loadVoiceManifest,
  pickClip,
  priorityOf,
  clampVolume,
  isEvent,
  PRIORITY,
  MAX_CONCURRENT,
  DEFAULT_VOLUME,
  MAX_VOLUME,
  CUE_COOLDOWN_MS,
  LINE_COOLDOWN_MS,
  LINE_BUDGET_MS,
};
