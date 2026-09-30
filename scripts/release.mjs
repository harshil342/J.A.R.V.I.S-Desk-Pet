#!/usr/bin/env node
// DeskPet release hook. No dependencies — Node 20 is already a build requirement.
//
//   node scripts/release.mjs check       preflight: clean tree, versions in sync, tag free
//   node scripts/release.mjs bump <patch|minor|major>
//   node scripts/release.mjs changelog   regenerate CHANGELOG.md from Conventional Commits
//   node scripts/release.mjs tag         annotated tag, message taken from the CHANGELOG section
//   node scripts/release.mjs push        push commit, then tag, then create the GitHub release
//   node scripts/release.mjs roll        move the windows-latest rolling tag to this release
//   node scripts/release.mjs --self-test
//
// Add --dry-run to any subcommand: prints the commands, executes nothing.
//
// Safety rules this file exists to enforce:
//   1. Refuses on a dirty tree. Never `git add -A`; stages named files only.
//   2. Version is single-sourced from clawd-on-desk/package.json; the sidecar and
//      CHANGELOG are made to match, or nothing is written.
//   3. The tag is annotated from the CHANGELOG section, not a hand-written string.
//   4. Push the commit before the tag, so a re-tag is a fast-forward, never a rewrite.
//   5. The release body is written to a file and passed with --notes-file.
//   6. Re-running after a partial failure resumes instead of double-tagging.
//   7. An unsigned Windows build creates a DRAFT release, never a public one.

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PKG = join(ROOT, "clawd-on-desk", "package.json");
const SIDECAR = join(ROOT, "minicpm-sidecar", "pyproject.toml");
const CHANGELOG = join(ROOT, "CHANGELOG.md");
const NOTES_TMP = join(ROOT, ".release-notes.tmp");

// The only place a version is authored. Everything else is derived.
const VERSION_FILE = "clawd-on-desk/package.json";
const ROLLING_TAG = "windows-latest";
const CHANGELOG_HEADER =
  "# Changelog\n\nAll notable changes to Deskpet. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n\nThe app and the sidecar ship as one product and carry the same version.\n";

const DRY = process.argv.includes("--dry-run");
const args = process.argv.slice(2).filter((a) => a !== "--dry-run");

// ── shell ────────────────────────────────────────────────────────────────────
function git(...argv) {
  return execFileSync("git", argv, { cwd: ROOT, encoding: "utf8" }).trim();
}
function gitDry(...argv) {
  return `git ${argv.join(" ")}`;
}
function run(argv, { quiet = false } = {}) {
  const cmd = argv.join(" ");
  if (DRY) { console.log(`  [dry] ${cmd}`); return ""; }
  if (!quiet) console.log(`  $ ${cmd}`);
  return execFileSync(argv[0], argv.slice(1), { cwd: ROOT, encoding: "utf8" }).trim();
}
function has(cmd) {
  try { execFileSync(cmd, ["--version"], { stdio: "ignore" }); return true; }
  catch { return false; }
}
function die(msg) {
  console.error(`\n  release: ${msg}\n`);
  process.exit(1);
}

// ── version sources ──────────────────────────────────────────────────────────
function readVersion() {
  return JSON.parse(readFileSync(PKG, "utf8")).version;
}
function readSidecarVersion() {
  // One line of TOML does not need a TOML parser.
  const m = readFileSync(SIDECAR, "utf8").match(/^version\s*=\s*"([^"]+)"/m);
  if (!m) die(`no version in ${SIDECAR}`);
  return m[1];
}
function nextVersion(cur, kind) {
  const [ma, mi, pa] = cur.split(".").map(Number);
  if (![ma, mi, pa].every(Number.isInteger)) die(`cannot parse version "${cur}"`);
  if (kind === "major") return `${ma + 1}.0.0`;
  if (kind === "minor") return `${ma}.${mi + 1}.0`;
  if (kind === "patch") return `${ma}.${mi}.${pa + 1}`;
  die(`bump needs one of: patch minor major (got "${kind}")`);
}

// ── conventional commits ─────────────────────────────────────────────────────
const SECTIONS = [
  [/^feat(!|\(|:)/, "Added"],
  [/^fix(!|\(|:)/, "Fixed"],
  [/^perf(!|\(|:)/, "Performance"],
  [/^refactor(!|\(|:)/, "Changed"],
  [/^(revert|security|test|docs)(!|\(|:)/, "Changed"],
];
// Internal-only types never appear in a user-facing changelog.
const INTERNAL = /^(chore|ci|build|style|test|docs|merge|revert)(\(|\!|:)/;

function sectionFor(subject) {
  for (const [re, name] of SECTIONS) if (re.test(subject)) return name;
  return null;
}
// "feat(onboarding): add X" -> "add X (onboarding)". The type prefix is noise
// in a user-facing changelog; the scope is not.
function humanize(subject) {
  const m = subject.match(/^(\w+)(?:\(([^)]*)\))?(!)?:\s*(.+)$/);
  if (!m) return subject;
  const scope = m[2] ? ` (${m[2]})` : "";
  return `${m[1] === "fix" ? "Fixed" : ""}${m[4].replace(/\.$/, "")}${scope}`.replace(/^Fixed/, "");
}
function isBreaking(subject, body) {
  return subject.includes("!") || /^BREAKING[ -]CHANGE:/m.test(body);
}
function commitsSince(tag) {
  const range = tag ? `${tag}..HEAD` : "HEAD";
  const raw = git("log", range, "--no-merges", "--pretty=format:%H%x1f%s%x1f%b%x1e");
  if (!raw) return [];
  return raw.split("\x1e").map((c) => c.replace(/^\s+/, "")).filter(Boolean).map((chunk) => {
    const [hash, subject, body = ""] = chunk.split("\x1f");
    return { hash, subject, body, breaking: isBreaking(subject, body), section: sectionFor(subject) };
  });
}
function lastTag() {
  const t = git("tag", "--sort=-v:refname", "--merged", "HEAD");
  return t ? t.split("\n")[0].trim() : null;
}

// ── changelog ────────────────────────────────────────────────────────────────
function changelogVersion(version) {
  if (!existsSync(CHANGELOG)) return null;
  const m = readFileSync(CHANGELOG, "utf8").match(new RegExp(`^## \\[?${version.replace(/\./g, "\\.")}\\]?`, "m"));
  return m ? m[0] : null;
}
function renderSection(version, commits) {
  const date = new Date().toISOString().slice(0, 10);
  const by = new Map();
  for (const c of commits) {
    if (!c.section || INTERNAL.test(c.subject)) continue;
    const list = by.get(c.section) ?? [];
    const text = humanize(c.subject);
    list.push(c.breaking ? `**breaking** ${text}` : text);
    by.set(c.section, list);
  }
  if (!by.size) return null;
  const order = ["Added", "Fixed", "Performance", "Changed"];
  const body = order
    .filter((s) => by.has(s))
    .map((s) => `### ${s}\n\n${[...new Set(by.get(s))].map((l) => `- ${l}`).join("\n")}`)
    .join("\n\n");
  return `## [${version}] - ${date}\n\n${body}\n`;
}
function writeChangelog(version) {
  const section = changelogSection(version);
  if (!section) die(`CHANGELOG.md has no section for ${version} — run "changelog" first`);
  const header = "# Changelog\n\nAll notable changes to DeskPet. Format follows Keep a Changelog.\n\n";
  const existing = existsSync(CHANGELOG) ? readFileSync(CHANGELOG, "utf8") : header;
  // Insert directly under the header, newest first.
  const at = existing.indexOf("## ");
  const next = at === -1 ? header + "\n" + section : existing.slice(0, at) + section + "\n" + existing.slice(at);
  if (!DRY) writeFileSync(CHANGELOG, next, "utf8");
  console.log(`  ${DRY ? "[dry] " : ""}wrote CHANGELOG.md section for ${version}`);
}
function changelogSection(version) {
  if (!existsSync(CHANGELOG)) return null;
  const text = readFileSync(CHANGELOG, "utf8");
  const re = new RegExp(`^## \\[?${version.replace(/\./g, "\\.")}\\]?[^\\n]*\\n([\\s\\S]*?)(?=\\n## |$)`, "m");
  const m = text.match(re);
  return m ? `## [${version}]${m[0].split("\n")[0].replace(/^## \[?[\d.]+\]?/, "").replace(/ - \d{4}-\d{2}-\d{2}$/, "")}\n${m[1]}` : null;
}

// ── tree state ───────────────────────────────────────────────────────────────
function isDirty() {
  return git("status", "--porcelain").length > 0;
}
// A signed build is a functional requirement, not a trust badge: electron-updater
// verifies the Authenticode signature of the downloaded .exe and rejects an
// unsigned one at install time. So an unsigned release must never go public.
function signingSource() {
  if (process.env.WIN_CSC_LINK || process.env.CSC_LINK) return "CSC_LINK";
  const dev = join(ROOT, "clawd-on-desk", "build", "deskpet-selfsigned.pfx");
  if (existsSync(dev) && process.env.DESKPET_CERT_PASSWORD !== undefined) return "dev-cert";
  return null;
}

// ── subcommands ──────────────────────────────────────────────────────────────
function check() {
  const problems = [];
  const v = readVersion();
  const sv = readSidecarVersion();
  console.log(`  version        ${v}`);
  console.log(`  sidecar        ${sv}`);
  console.log(`  last tag       ${lastTag() ?? "(none)"}`);
  console.log(`  tree           ${isDirty() ? "DIRTY" : "clean"}`);
  console.log(`  gh cli         ${has("gh") ? "present" : "MISSING"}`);
  console.log(`  signing cert   ${signingSource() ? "configured" : "ABSENT - release will be a draft"}`);
  if (sv !== v) problems.push(`sidecar is ${sv}, app is ${v} — they ship as one product and must match`);
  if (isDirty()) problems.push("tree is dirty — commit or stash before releasing");
  if (!has("gh")) problems.push("gh CLI not found, cannot create the release page");
  if (!changelogVersion(v)) problems.push(`CHANGELOG.md has no section for ${v}`);
  if (problems.length) {
    console.error("\n  check FAILED:");
    for (const p of problems) console.error(`    - ${p}`);
    process.exit(1);
  }
  console.log("  check OK\n");
}

function bump(kind) {
  if (isDirty()) die("tree is dirty — commit or stash first");
  const from = readVersion();
  const to = nextVersion(from, kind);
  const commits = commitsSince(lastTag());
  if (kind === "auto") {
    const auto = commits.some((c) => c.breaking) ? "major" : commits.some((c) => c.section === "Added") ? "minor" : "patch";
    console.log(`  suggested bump ${auto} (${commits.length} commits since ${lastTag() ?? "root"})`);
  }
  console.log(`  ${from} -> ${to}`);
  // Single source: write all three or none.
  if (!DRY) {
    const pkg = JSON.parse(readFileSync(PKG, "utf8"));
    pkg.version = to;
    writeFileSync(PKG, JSON.stringify(pkg, null, 2) + "\n", "utf8");
    const side = readFileSync(SIDECAR, "utf8").replace(/^version\s*=\s*"[^"]+"/m, `version = "${to}"`);
    writeFileSync(SIDECAR, side, "utf8");
  }
  run(["git", "add", VERSION_FILE, "minicpm-sidecar/pyproject.toml"]);
  console.log(`  bumped to ${to} (staged, not committed)`);
  return to;
}

// Insert newest-first, but below an existing [Unreleased] block: that section is
// a staging area for work with no version yet and always reads first.
//
// Block boundaries are found with indexOf("\n## ") rather than a regex. A
// lookahead with the /m flag stops at the first line end, which orphaned the
// Unreleased body below the section it belongs to.
function insertSection(existing, section) {
  if (!existing.includes("## ")) return CHANGELOG_HEADER + "\n" + section + "\n";
  const start = existing.indexOf("## [Unreleased]");
  if (start === -1) {
    const first = existing.indexOf("## ");
    return existing.slice(0, first) + section + "\n" + existing.slice(first);
  }
  const bodyStart = existing.indexOf("\n", start) + 1;
  const next = existing.indexOf("\n## ", bodyStart);
  const at = next === -1 ? existing.length : next;
  return `${existing.slice(0, at)}\n${section}\n${existing.slice(at)}`;
}

function changelog() {
  const v = readVersion();
  if (changelogVersion(v)) die(`CHANGELOG.md already has a section for ${v}`);
  const section = renderSection(v, commitsSince(lastTag()));
  if (!section) die("no releasable commits since the last tag (only chore/ci/docs/test)");
  if (!DRY) {
    const existing = existsSync(CHANGELOG) ? readFileSync(CHANGELOG, "utf8") : CHANGELOG_HEADER + "\n";
    writeFileSync(CHANGELOG, insertSection(existing, section), "utf8");
  }
  console.log(`  ${DRY ? "[dry] " : ""}wrote CHANGELOG.md for ${v}`);
}

function tag() {
  const v = readVersion();
  const name = `v${v}`;
  if (git("tag", "--list", name)) die(`tag ${name} already exists — bump first`);
  const notes = changelogSection(v);
  if (!notes) die(`CHANGELOG.md has no section for ${v} — run "changelog" first`);
  if (!DRY) writeFileSync(NOTES_TMP, notes, "utf8");
  run(["git", "tag", "-a", name, "-F", NOTES_TMP]);
  console.log(`  tagged ${name}`);
}

function push() {
  const v = readVersion();
  const name = `v${v}`;
  if (!git("tag", "--list", name)) die(`tag ${name} does not exist — run "tag" first`);
  if (!has("gh")) die("gh CLI not found");
  // Commit first, then tag: this ordering keeps a re-tag a fast-forward.
  run(["git", "push", "origin", "HEAD:main"]);
  run(["git", "push", "origin", name]);
  const notes = changelogSection(v);
  if (!DRY) writeFileSync(NOTES_TMP, notes, "utf8");
  const draft = signingSource() ? [] : ["--draft"];
  if (!signingSource()) console.log("  ! no signing cert - creating a DRAFT release, not a public one");
  run(["gh", "release", "create", name, "--title", `DeskPet ${name}`, "--notes-file", NOTES_TMP, "--verify-tag", ...draft]);
  console.log(`  released ${name}`);
}

function roll() {
  const v = readVersion();
  const name = `v${v}`;
  if (!git("tag", "--list", name)) die(`tag ${name} does not exist — run "tag" and "push" first`);
  // The only force-push in this script, and it is the point of a rolling channel:
  // this tag always names the newest build, so users can install without a version.
  console.log(`  moving rolling tag ${ROLLING_TAG} -> ${name} (force, by design)`);
  run(["git", "tag", "-f", ROLLING_TAG, name]);
  run(["git", "push", "origin", "--force", `refs/tags/${ROLLING_TAG}`]);
  console.log(`  installer URL: releases/download/${ROLLING_TAG}/Deskpet-Setup.exe`);
}

// ── finalize: sign locally, then publish ─────────────────────────────────────
//
// Why this is a separate step rather than part of `push`:
//
// electron-builder's verifyUpdateCodeSignature defaults to true, so an
// *unsigned installed app* makes autoUpdater.checkForUpdates() throw. Signing
// in CI would need the private key in GitHub Actions secrets, which puts a
// signing key on a third-party service. So: CI builds, this signs, and the
// release stays a DRAFT until a human has signed it. The key never leaves the
// machine it was generated on.
//
// The rolling channel also gets its own asset, because a rolling tag with no
// asset is a 404 - moving the tag alone was the bug that made the first version
// of this channel useless.
const WORK = join(ROOT, ".release-work");

function finalize() {
  const v = readVersion();
  const name = `v${v}`;
  if (!has("gh")) die("gh CLI not found");
  if (!has("git")) die("git not found");

  // 1. Refuse to publish anything that is not currently a draft. Re-running
  //    after a successful finalize must not silently re-sign and re-upload.
  //    A missing release is a normal state (nothing is tagged yet), so it gets a
  //    plain message rather than gh's stack trace.
  let draft = "";
  try {
    draft = run(["gh", "release", "view", name, "--json", "isDraft", "--jq", ".isDraft"]);
  } catch {
    die(`no GitHub release named ${name} yet. Run "push" first - it creates the draft.`);
  }
  if (!/true/.test(draft)) {
    die(`${name} is not a draft. A published release is immutable here by design; ` +
        "if you need to redo it, delete the release and push the tag again.");
  }

  if (DRY) {
    console.log("  (dry run) would: download the CI artifact, sign it, upload it,");
    console.log("  (dry run)        publish the release, then move windows-latest.");
    return;
  }

  // 2. Fetch the built installer from the CI run that made this release.
  if (existsSync(WORK)) rmSync(WORK, { recursive: true, force: true });
  mkdirSync(WORK, { recursive: true });
  console.log("  downloading the CI build for " + name);
  const runId = run(["gh", "run", "list", "--workflow", "release.yml", "--limit", "20",
    "--json", "headBranch,conclusion,databaseId", "--jq",
    `.[] | select(.headBranch == "${name}") | .databaseId`]).split(/\r?\n/).filter(Boolean)[0];
  if (!runId) die(`no release.yml run found for ${name}. Has CI finished?`);
  run(["gh", "run", "download", runId, "--name", "windows-installer", "--dir", WORK]);

  // 3. Sign every installer, locally, with the self-signed certificate.
  const installer = existsSync(WORK)
    ? execFileSync("cmd", ["/c", "dir", "/b", join(WORK, "*.exe")], { encoding: "utf8" })
        .split(/\r?\n/).map((s) => s.trim()).filter(Boolean)[0]
    : undefined;
  if (!installer) die(`no .exe in the downloaded artifact. Looked in ${WORK}`);
  const exe = join(WORK, installer);
  console.log(`  signing ${installer} with the local certificate`);
  run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
    join(ROOT, "clawd-on-desk", "scripts", "sign-artifact.ps1"), "-Path", exe]);

  // 4. Re-upload, replacing the unsigned asset of the same name.
  run(["gh", "release", "upload", name, exe, "--clobber"]);

  // 5. Publish. Only now is a signed build publicly reachable.
  run(["gh", "release", "edit", name, "--draft=false"]);
  console.log(`  published ${name}`);

  // 6. Move the rolling channel and give it a stable asset of its own.
  const stable = join(WORK, "Deskpet-Setup.exe");
  execFileSync("cmd", ["/c", "copy", "/y", exe, stable], { stdio: "ignore" });
  run(["git", "tag", "-f", ROLLING_TAG, name]);
  run(["git", "push", "origin", "--force", `refs/tags/${ROLLING_TAG}`]);
  run(["gh", "release", "upload", ROLLING_TAG, stable, "--clobber"]);
  console.log(`  ${ROLLING_TAG} -> ${name} as Deskpet-Setup.exe`);
  console.log(`  install URL: releases/download/${ROLLING_TAG}/Deskpet-Setup.exe`);
}

// ── self-test: the logic that decides what ships ─────────────────────────────
function selfTest() {
  let pass = 0;
  const t = (name, got, want) => {
    if (got === want) { pass++; return; }
    console.error(`  FAIL ${name}\n    got  ${JSON.stringify(got)}\n    want ${JSON.stringify(want)}`);
    process.exitCode = 1;
  };
  t("patch bump", nextVersion("0.12.3", "patch"), "0.12.4");
  t("minor bump", nextVersion("0.12.3", "minor"), "0.13.0");
  t("major bump", nextVersion("0.12.3", "major"), "1.0.0");
  t("no 9s", nextVersion("0.9.9", "minor"), "0.10.0");

  t("feat -> Added", sectionFor("feat: add wake scheduling"), "Added");
  t("fix -> Fixed", sectionFor("fix(core): div by zero"), "Fixed");
  t("breaking suffix still Added", sectionFor("feat!: drop node 18"), "Added");
  t("chore is internal", INTERNAL.test("chore: bump version"), true);
  t("ci is internal", INTERNAL.test("ci: gate builds on tests"), true);
  t("feat is not internal", INTERNAL.test("feat: add wake scheduling"), false);

  const c = (subject, body = "") => ({ subject, body, breaking: isBreaking(subject, body), section: sectionFor(subject) });
  t("bang is breaking", c("feat!: x").breaking, true);
  t("BREAKING CHANGE body is breaking", c("feat: x", "BREAKING CHANGE: y").breaking, true);
  t("plain feat is not breaking", c("feat: x").breaking, false);

  t("humanize drops the type prefix", humanize("feat(onboarding): add X"), "add X (onboarding)");
  t("humanize keeps a plain subject", humanize("fix(core): div by zero"), "div by zero (core)");
  t("humanize handles no scope", humanize("chore: bump version"), "bump version");

  t("chore filtered out of changelog", renderSection("1.0.0", [c("chore: bump"), c("ci: gate")]), null);
  const rendered = renderSection("1.0.0", [c("feat: a"), c("fix(core): b")]);
  t("Added before Fixed", rendered.indexOf("### Added") < rendered.indexOf("### Fixed"), true);
  t("has date", /## \[1\.0\.0\] - \d{4}-\d{2}-\d{2}/.test(rendered), true);
  t("dedupes identical subjects", (rendered.match(/^- a$/gm) || []).length, 1);

  // A new section must land below [Unreleased], never above it, and must not
  // orphan the Unreleased body.
  const doc = `${CHANGELOG_HEADER}\n## [Unreleased]\n\nwork in progress\n\n## [0.11.0] - 2026-08-28\n\n- old\n`;
  const inserted = insertSection(doc, "## [0.12.0] - 2026-09-30\n\n### Fixed\n\n- x\n");
  t("Unreleased stays first", inserted.indexOf("## [Unreleased]") < inserted.indexOf("## [0.12.0]"), true);
  t("Unreleased body stays under its heading", inserted.indexOf("work in progress") < inserted.indexOf("## [0.12.0]"), true);
  t("older section still last", inserted.indexOf("## [0.11.0]") > inserted.indexOf("## [0.12.0]"), true);
  t("no Unreleased means newest goes on top", insertSection(`${CHANGELOG_HEADER}\n## [0.11.0] - x\n\n- old\n`, "## [0.12.0] - y\n\n- new\n").indexOf("## [0.12.0]") < insertSection(`${CHANGELOG_HEADER}\n## [0.11.0] - x\n\n- old\n`, "## [0.12.0] - y\n\n- new\n").indexOf("## [0.11.0]"), true);

  console.log(`  self-test: ${pass} passed${process.exitCode ? ", some failed" : ""}`);
}

const cmd = args[0];
const table = { check, bump, changelog, tag, push, roll, finalize };
if (cmd === "--self-test") { selfTest(); process.exit(process.exitCode ?? 0); }
if (!table[cmd]) {
  console.log(`usage: node scripts/release.mjs <check|bump|changelog|tag|push|finalize|roll|--self-test> [--dry-run]`);
  console.log(`\n  finalize  sign the CI build locally, publish the draft, move ${ROLLING_TAG}`);
  process.exit(1);
}
table[cmd](args[1]);
if (existsSync(NOTES_TMP) && cmd !== "push" && cmd !== "tag") rmSync(NOTES_TMP, { force: true });
