#!/usr/bin/env node
/**
 * Regression gate for UI quality findings.
 *
 * The detector itself (impeccable, Apache-2.0) needs no LLM and no API key, so
 * this runs in CI like any other check. The count only has to go down, which is
 * the whole point: a new anti-pattern must be a deliberate decision, not
 * something that arrives unnoticed.
 *
 *   node scripts/ui-detect.mjs            check against the recorded ceiling
 *   node scripts/ui-detect.mjs --write    re-record the ceiling after a
 *                                         deliberate change
 *   node scripts/ui-detect.mjs --print    show findings, do not gate
 *
 * The ceiling lives in ui-detect-baseline.json next to this file.
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..");
const BASELINE = join(HERE, "ui-detect-baseline.json");
const DETECTOR = join(
  REPO,
  ".opencode/skills/impeccable/scripts/bin/windows-x64/impeccable.exe"
);
const TARGET = join(REPO, "clawd-on-desk/src");

const args = new Set(process.argv.slice(2));

if (!existsSync(DETECTOR)) {
  if (args.has("--print")) {
    console.log("detector not installed - run: npx impeccable install --providers=opencode --scope=project --no-hooks");
    process.exit(0);
  }
  // A missing detector must not silently pass the gate, and must not fail the
  // build either: the tool is deliberately not committed (14 MB binary). The
  // count is still checked whenever the detector is present.
  console.warn("impeccable detector not installed - UI check skipped.");
  console.warn("  npx impeccable install --providers=opencode --scope=project --no-hooks");
  process.exit(0);
}

let report = "";
try {
  report = execFileSync(DETECTOR, ["detect", TARGET], {
    cwd: REPO,
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
    // Capture stderr instead of inheriting it, so the detector's own chatter
    // does not scroll past the summary this script is supposed to print.
    stdio: ["ignore", "pipe", "pipe"],
  });
} catch (err) {
  // The detector exits non-zero when it finds something; that is not a crash.
  report = `${err.stdout ?? ""}${err.stderr ?? ""}`;
  if (!report) {
    console.error("UI detector failed to run:", err.message);
    process.exit(1);
  }
}

// Count rule tags, e.g. "[dark-glow] ...".
const counts = {};
for (const match of report.matchAll(/\[([a-z0-9-]+)\]/g)) {
  counts[match[1]] = (counts[match[1]] ?? 0) + 1;
}
const total = Object.values(counts).reduce((a, b) => a + b, 0);

if (args.has("--print")) {
  console.log(report);
  console.log(`\ntotal: ${total}`);
  process.exit(0);
}

if (args.has("--write")) {
  writeFileSync(BASELINE, JSON.stringify({ total, counts }, null, 2) + "\n", "utf8");
  console.log(`recorded UI baseline: ${total} findings`);
  for (const [rule, n] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(3)}  ${rule}`);
  }
  process.exit(0);
}

let baseline = { total: Infinity, counts: {} };
if (existsSync(BASELINE)) {
  baseline = JSON.parse(readFileSync(BASELINE, "utf8"));
}

console.log(`UI findings: ${total} (ceiling ${baseline.total})`);
for (const [rule, n] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
  console.log(`  ${String(n).padStart(3)}  ${rule}`);
}

if (total > baseline.total) {
  console.error(
    `\nFAIL: ${total - baseline.total} new UI anti-pattern(s) above the recorded ceiling of ${baseline.total}.`
  );
  console.error("See docs/ui-audit.md for what is accepted and why.");
  console.error("If the change is deliberate: node scripts/ui-detect.mjs --write");
  process.exit(1);
}

console.log("OK: no new UI anti-patterns.");
