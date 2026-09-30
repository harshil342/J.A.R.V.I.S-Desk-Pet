#!/usr/bin/env node
"use strict";

// Syntax check every JavaScript source file.
//
// This is deliberately not ESLint. The repo has 182 JS modules and no config
// file, no formatter, and no `lint` script; adding a linter at this stage would
// mean either fixing thousands of pre-existing findings or turning it off. What
// actually catches the class of problem that reaches a packaged build is a
// parse error, and `node --check` is free, needs no config, and needs no
// dependency. If this ever grows a real linter, this script should go.
//
// Exits non-zero with the offending files listed.

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const APP = path.join(__dirname, "..");
const ROOTS = ["src", "hooks", "tools", "scripts", "test"];
const SKIP = new Set(["node_modules", "dist", "build", "bin", "assets", "themes", "pwa"]);

function walk(dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    if (e.isDirectory()) {
      if (SKIP.has(e.name) || e.name.startsWith(".")) continue;
      walk(path.join(dir, e.name), out);
    } else if (/\.(js|cjs|mjs)$/.test(e.name)) {
      out.push(path.join(dir, e.name));
    }
  }
  return out;
}

const files = ROOTS.flatMap((r) => walk(path.join(APP, r)));
// The Electron test runner's own entry point uses ESM-flavoured syntax in .js
// files under a CommonJS package, which node --check rejects. Skip test/ for
// the same reason the runner does: those are loaded through node:test.
const checkable = files.filter((f) => !f.includes(`${path.sep}test${path.sep}`));

const bad = [];
for (const file of checkable) {
  try {
    execFileSync(process.execPath, ["--check", file], { stdio: "pipe" });
  } catch (err) {
    const first = String(err.stderr || err.stdout || "")
      .split("\n")
      .map((l) => l.trim())
      .find((l) => l && !l.startsWith("at "));
    bad.push({ file: path.relative(APP, file), reason: first || "parse error" });
  }
}

if (bad.length) {
  console.error(`\n${bad.length} file(s) failed to parse:\n`);
  for (const b of bad) console.error(`  ${b.file}\n    ${b.reason}`);
  process.exit(1);
}
console.log(`syntax OK: ${checkable.length} files parsed, ${files.length - checkable.length} test files skipped`);
