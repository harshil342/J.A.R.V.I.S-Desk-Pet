"use strict";

// Cross-file invariants: values that are written in more than one place, and
// which silently disagreed before. Same pattern as remote-ssh-deploy.test.js,
// which asserts set equality between the shell script's FILES=() array and
// remote-ssh-deploy.js's HOOK_FILES.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const APP = path.join(__dirname, "..");
const REPO = path.join(APP, "..");
const SRC = path.join(APP, "src");
const HOOKS = path.join(APP, "hooks");

const read = (...parts) => fs.readFileSync(path.join(...parts), "utf8");

// The prompt measured 4925 tokens against a 4096 window, which turned native
// tool calling off by itself 12 times in one day with no user-visible sign.
const CTX_DEFAULT = 8192;

describe("MINICPM_CTX default is the same everywhere", () => {
  // Paths are relative to the repo root, not the app root: two of the four
  // sites live in the Python sidecar.
  const sites = [
    ["clawd-on-desk/src/minicpm-chat.js", /Number\(process\.env\.MINICPM_CTX\) \|\| (\d+)/],
    ["minicpm-sidecar/gateway/llama_client.py", /ctx_size: int = (\d+)/],
    ["minicpm-sidecar/gateway/server.py", /ctx_size: int = (\d+)/],
    ["minicpm-sidecar/gateway/__main__.py", /MINICPM_CTX", "(\d+)"/],
  ];

  for (const [label, pattern] of sites) {
    it(`${label} defaults to ${CTX_DEFAULT}`, () => {
      const text = read(REPO, label);
      const found = text.match(pattern);
      assert.ok(found, `no ctx default matched in ${label}`);
      assert.equal(Number(found[1]), CTX_DEFAULT, `${label} ctx default drifted`);
    });
  }
});

describe("the auto-start hook spawns the real executable", () => {
  const pkg = JSON.parse(read(APP, "package.json"));
  const productName = pkg.build && pkg.build.productName;

  it("package.json declares a productName", () => {
    assert.ok(productName, "build.productName is required to locate the executable");
  });

  it("auto-start.js derives the exe from productName, not a literal", () => {
    const text = read(HOOKS, "auto-start.js");
    assert.ok(
      text.includes("build.productName"),
      "auto-start.js must read build.productName so a rename cannot orphan it"
    );
  });

  it("auto-start.js hardcodes no stale executable name", () => {
    // Comments are stripped first: the fix is explained in a comment that has
    // to name the old literals to be useful, and prose is not a code path.
    // Split on /\r?\n/ rather than "\n" — `.` does not match \r in JS, so a
    // CRLF file would defeat the strip and fail the assertion.
    const code = read(HOOKS, "auto-start.js")
      .split(/\r?\n/)
      .map((line) => line.replace(/\/\/.*$/, ""))
      .join("\n");
    for (const stale of ["MiniCPM Desk Pet", "Clawd on Desk", "clawd-on-desk"]) {
      assert.ok(!code.includes(stale), `auto-start.js still builds a path from: ${stale}`);
    }
  });
});

describe("no theme ships audio the app no longer has", () => {
  it("DEFAULT_SOUNDS is empty", () => {
    const schema = read(SRC, "theme-schema.js");
    const m = schema.match(/const DEFAULT_SOUNDS = (\{[^}]*\})/);
    assert.ok(m, "DEFAULT_SOUNDS not found");
    assert.equal(m[1].replace(/\s/g, ""), "{}", "themes must supply their own audio");
  });

  it("the old global sound assets are gone", () => {
    assert.ok(
      !fs.existsSync(path.join(APP, "assets", "sounds", "complete.mp3")),
      "complete.mp3 was removed; a theme referencing it would resolve to silence"
    );
  });
});
