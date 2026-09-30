const assert = require("node:assert");
const { describe, it } = require("node:test");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const HOOK = path.join(ROOT, "scripts", "release.mjs");
const src = fs.readFileSync(HOOK, "utf8");

const run = (...argv) =>
  spawnSync(process.execPath, [HOOK, ...argv], { cwd: ROOT, encoding: "utf8" });

describe("release hook: finalize", () => {
  it("is registered and documented in the usage line", () => {
    const out = run().stdout + run().stderr;
    assert.match(out, /finalize/, "usage should list finalize");
    assert.match(src, /table = \{[^}]*finalize/, "finalize should be in the command table");
  });

  it("refuses to publish a release that is not a draft", () => {
    // The safety property: a second `finalize` must not silently re-sign and
    // re-upload over a release that is already public. There is no v0.12.0
    // release in this repo, so this exercises the missing-release path too.
    const r = run("finalize");
    assert.strictEqual(r.status, 1, "should exit non-zero");
    const msg = r.stdout + r.stderr;
    assert.match(msg, /no GitHub release named|is not a draft/, `unhelpful failure: ${msg}`);
    assert.doesNotMatch(
      msg,
      /at Object\.|node:internal/,
      "should fail with a message, not a stack trace"
    );
  });

  it("keeps --dry-run free of side effects", () => {
    const r = run("finalize", "--dry-run");
    // It may still refuse on the precondition, but it must not have tried to
    // download, sign, upload or move a tag.
    const msg = r.stdout + r.stderr;
    assert.doesNotMatch(msg, /windows-latest ->/, "dry run must not move the rolling tag");
    assert.doesNotMatch(msg, /published v/, "dry run must not publish");
  });

  it("signs before publishing, never the other way round", () => {
    // Ordering is the whole point: publishing first would put an unsigned
    // installer in front of users, and a rolling tag moves last of all.
    const body = src.slice(src.indexOf("function finalize"));
    const sign = body.indexOf("sign-artifact.ps1");
    const publish = body.indexOf("--draft=false");
    const roll = body.indexOf("refs/tags/");
    assert.ok(sign > 0 && publish > 0 && roll > 0, "all three steps should exist");
    assert.ok(sign < publish, "sign before publishing");
    assert.ok(publish < roll, "publish before moving the rolling channel");
  });

  it("requires the local certificate rather than a CI secret", () => {
    // The reason finalize exists at all: the key stays on this machine.
    assert.match(src, /sign-artifact\.ps1/);
    assert.doesNotMatch(
      src,
      /WIN_CSC_KEY_PASSWORD/,
      "the release hook should not read a CI signing secret"
    );
  });

  it("still passes its own self-test", () => {
    const out = execFileSync(process.execPath, [HOOK, "--self-test"], {
      cwd: ROOT,
      encoding: "utf8",
    });
    assert.match(out, /self-test: \d+ passed/);
    assert.doesNotMatch(out, /failed/, "self-test must not report failures");
  });
});
