const assert = require("node:assert");
const { describe, it } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { minimatch } = require("minimatch");
const { execFileSync } = require("node:child_process");

const pkg = require("../package.json");
const ROOT = path.join(__dirname, "..");

function matchedByAnyGlob(globs, target) {
  return globs.some((g) => minimatch(target, g));
}

describe("package build config", () => {
  it("ships project window icons in packaged builds", () => {
    assert.ok(
      pkg.build.files.includes("assets/icons/**/*"),
      "build.files should include assets/icons/**/*"
    );
  });

  it("ships agent session icons in packaged builds", () => {
    assert.ok(
      pkg.build.files.includes("assets/icons/agents/**/*"),
      "build.files should include assets/icons/agents/**/*"
    );
  });

  it("ships third-party notices in packaged builds", () => {
    assert.ok(
      pkg.build.files.includes("NOTICE.md"),
      "build.files should include NOTICE.md"
    );
  });

  it("unpacks built-in theme assets so the folder can be opened from settings", () => {
    assert.ok(
      pkg.build.asarUnpack.includes("assets/svg/**/*"),
      "asarUnpack should include assets/svg/**/*"
    );
    assert.ok(
      pkg.build.asarUnpack.includes("themes/**/*"),
      "asarUnpack should include themes/**/*"
    );
  });

  it("ships and unpacks runtime files required by external hook scripts", () => {
    assert.ok(
      pkg.build.files.includes("hooks/**/*"),
      "build.files should include hooks/**/*"
    );
    assert.ok(
      pkg.build.files.includes("extensions/**/*"),
      "build.files should include extensions/**/*"
    );
    assert.ok(
      pkg.build.files.includes("agents/**/*"),
      "build.files should include agents/**/*"
    );
    assert.ok(
      pkg.build.asarUnpack.includes("agents/**/*"),
      "asarUnpack should include agents/**/*"
    );
    assert.ok(
      pkg.build.asarUnpack.includes("hooks/**/*"),
      "asarUnpack should include hooks/**/*"
    );
    assert.ok(
      pkg.build.asarUnpack.includes("extensions/**/*"),
      "asarUnpack should include extensions/**/*"
    );
  });

  // Decision D3: Windows x64 is the only supported target. These used to pin
  // x64+arm64 across three platforms, which is how a build that shipped x64
  // binaries inside an arm64 installer, and a README link to a macOS DMG the
  // pipeline could never produce, survived this long.
  describe("Windows is the only build target (D3)", () => {
    it("builds exactly one Windows architecture, x64", () => {
      const targets = pkg.build.win && pkg.build.win.target;
      assert.ok(Array.isArray(targets), "build.win.target should be an array");
      const nsis = targets.find((t) => t && t.target === "nsis");
      assert.ok(nsis, "build.win.target should include an nsis target");
      // electron-builder has no per-arch extraResources, so a second arch
      // would silently receive win-x64 native binaries.
      assert.deepStrictEqual(
        nsis.arch,
        ["x64"],
        "only x64 may be declared: extraResources cannot vary per arch, so a " +
        "second arch would ship x64 llama-server.exe/minicpm-sidecar.exe"
      );
    });

    it("declares no macOS or Linux target", () => {
      assert.strictEqual(pkg.build.mac, undefined, "macOS is not supported (D3)");
      assert.strictEqual(pkg.build.linux, undefined, "Linux is not supported (D3)");
    });

    it("keeps ${arch} in the installer name", () => {
      assert.match(
        pkg.build.win.artifactName,
        /\$\{arch\}/,
        "artifactName must include ${arch} so the name stays explicit"
      );
    });

    it("does not emit a universal installer", () => {
      assert.strictEqual(pkg.build.nsis && pkg.build.nsis.buildUniversalInstaller, false);
    });
  });

  describe("build scripts match the declared target", () => {
    it("has an x64 script and no arm64-only one", () => {
      assert.strictEqual(pkg.scripts["build:win:x64"], "electron-builder --win nsis:x64");
      assert.ok(
        !pkg.scripts["build:win:arm64"] && !/arm64/.test(pkg.scripts["build:win:all"] || ""),
        "arm64 build scripts must go with the dropped target, or they will " +
        "produce an installer with x64 native binaries"
      );
    });

    it("has a signed build entry point", () => {
      assert.match(pkg.scripts["build:win:signed"] || "", /build-signed\.ps1/,
        "signing is a functional requirement: electron-updater rejects an " +
        "unsigned artifact, so there must be one command that builds signed");
    });
  });

  // getWindowsShellIconPath has a three-step fallback:
  //   1. resourcesPath/icon.ico            ← extraResources copy
  //   2. resourcesPath/app.asar.unpacked/assets/icon.ico
  //   3. resourcesPath/app.asar/assets/icon.ico
  // Fallback 1 only works if extraResources actually copies icon.ico, and
  // fallback 3 only works if icon.ico is inside build.files. Guard both so a
  // future refactor to either array can't silently drop the shell icon.
  describe("Windows shell icon fallback chain", () => {
    it("has the source icon.ico on disk", () => {
      const src = path.join(ROOT, "assets", "icon.ico");
      assert.ok(fs.existsSync(src), "assets/icon.ico must exist for build.win.icon + extraResources");
    });

    it("copies icon.ico into resourcesPath via extraResources", () => {
      const extra = pkg.build.extraResources || [];
      const copied = extra.some(
        (e) => e && e.from === "assets/icon.ico" && e.to === "icon.ico"
      );
      assert.ok(copied, "build.extraResources must copy assets/icon.ico → icon.ico (shell fallback 1)");
    });

    it("wires win.icon to the same source file", () => {
      assert.strictEqual(
        pkg.build.win && pkg.build.win.icon,
        "assets/icon.ico",
        "build.win.icon should point at the same file the shell icon chain expects"
      );
    });

    it("packs icon.ico into the asar so fallback 3 resolves", () => {
      // getWindowsShellIconPath's third fallback reads
      // resourcesPath/app.asar/assets/icon.ico — which only exists if the
      // file survives the build.files glob filter. Earlier versions listed
      // only assets/icons/**/* (subdir), which does NOT match assets/icon.ico
      // at the root, so fallback 3 was dead. Guard against that regression.
      assert.ok(
        matchedByAnyGlob(pkg.build.files, "assets/icon.ico"),
        "build.files must include a glob covering assets/icon.ico (fallback 3)"
      );
    });
  });

  describe("Sidecar and adapter packaging", () => {
    it("does not preflight remote approval sidecars during source launches", () => {
      assert.strictEqual(
        pkg.scripts.start,
        "node launch.js",
        "source launch should not fetch or verify remote-control sidecars automatically"
      );
      assert.match(pkg.scripts.prebuild, /node scripts\/verify-adapters\.js/);
      assert.strictEqual(pkg.scripts["fetch:adapters"], "node scripts/fetch-adapters.js");
    });

    it("copies bundled bin and adapters into packaged resources", () => {
      const extra = pkg.build.extraResources || [];
      const binCopied = extra.some(
        (e) => e && e.from === "bin" && e.to === "bin"
      );
      const adaptersCopied = extra.some(
        (e) => e && e.from === "../adapters" && e.to === "adapters"
      );
      assert.ok(binCopied, "build.extraResources must copy bin -> bin");
      assert.ok(adaptersCopied, "build.extraResources must copy ../adapters -> adapters");
    });
  });

  // These two used to read .github/workflows/build.yml inside clawd-on-desk/,
  // which GitHub Actions never executed: only <repo-root>/.github/workflows is
  // read, and a nested directory is ignored entirely. So they were asserting
  // against a dead file. They now assert the pipeline that actually runs.
  describe("GitHub release publication", () => {
    // REPO_ROOT, not ROOT: GitHub only reads <repo-root>/.github/workflows, and
    // ROOT is clawd-on-desk/. A copy of these workflows under clawd-on-desk/.github
    // used to exist and was never executed by anything.
    const REPO_ROOT = path.join(ROOT, "..");
    const releaseWorkflow = () =>
      fs.readFileSync(path.join(REPO_ROOT, ".github", "workflows", "release.yml"), "utf8");

    it("gates publication on the test job", () => {
      const workflow = releaseWorkflow();
      assert.ok(/^  test:/m.test(workflow), "release.yml should define a test job");
      const buildWindows = workflow.indexOf("  build-windows:");
      const release = workflow.indexOf("  release:");
      assert.ok(buildWindows >= 0 && release >= 0);
      // needs: must list test, or a failing suite still ships a release.
      assert.match(
        workflow.slice(release, release + 200),
        /needs:\s*\[[^\]]*test[^\]]*\]/,
        "the release job must depend on the test job"
      );
      assert.ok(
        buildWindows < release,
        "builds must complete before publication"
      );
    });

    it("refuses to publish an unsigned build as public", () => {
      const workflow = releaseWorkflow();
      const gate = workflow.indexOf("steps.gate.outputs.signed");
      assert.ok(gate >= 0, "publication should be gated on a signing certificate");
      assert.ok(
        /draft:\s*\$\{\{\s*steps\.gate\.outputs\.signed == 'false'\s*\}\}/.test(workflow),
        "an unsigned build must create a DRAFT, never a public release: " +
          "electron-updater rejects an unsigned .exe at install time, so a " +
          "public one would strand every existing user"
      );
      assert.match(
        workflow,
        /fail_on_unmatched_files:\s*true/,
        "a release with zero artifacts should fail rather than publish empty"
      );
    });

    it("does not hardcode a version into the release title", () => {
      const workflow = releaseWorkflow();
      const nameIndex = workflow.indexOf("name: Deskpet");
      assert.ok(nameIndex >= 0, "release should have a name line");
      const line = workflow.slice(nameIndex, workflow.indexOf("\n", nameIndex));
      // The old workflow shipped a title pinned to v0.11.0, so pushing v0.12.0
      // produced a release titled v0.11.0.
      assert.ok(
        !/v0\.\d+\.\d+/.test(line),
        `release title must derive from the tag, found: ${line.trim()}`
      );
    });

    it("moves the rolling channel only for public releases", () => {
      const workflow = releaseWorkflow();
      const roll = workflow.indexOf("  roll:");
      assert.ok(roll >= 0, "release.yml should define a roll job");
      const step = workflow.slice(roll);
      assert.ok(
        /if:\s*needs\.release\.outputs\.draft != 'true'/.test(step),
        "windows-latest must not be pointed at a draft"
      );
      assert.ok(
        /windows-latest/.test(step) && /--force/.test(step),
        "the roll job should force-move the rolling tag"
      );
    });

    it("publishes a stable installer name to the rolling channel", () => {
      // Moving the rolling tag is not enough. The versioned asset lives on the
      // version tag, so windows-latest/Deskpet-<version>-x64.exe 404s forever.
      // The channel only earns its name if it carries an asset of its own.
      const workflow = releaseWorkflow();
      const roll = workflow.indexOf("  roll:");
      assert.ok(roll >= 0, "release.yml should define a roll job");
      const step = workflow.slice(roll);
      assert.ok(
        /tag_name:\s*windows-latest/.test(step),
        "the roll job should upload an asset to the windows-latest tag"
      );
      assert.ok(
        /Deskpet-Setup\.exe/.test(step),
        "the rolling channel should publish the stable Deskpet-Setup.exe name"
      );
      assert.ok(
        /actions\/download-artifact/.test(step),
        "the roll job needs the built installer, so it must download the artifact"
      );
      // Uploading before moving the tag would publish the asset onto the *old*
      // release. Upload first, then move.
      assert.ok(
        step.indexOf("Deskpet-Setup.exe") < step.indexOf("git tag -f"),
        "upload the stable asset before moving the rolling tag"
      );
    });

    it("keeps the install docs pointing at the stable URL", () => {
      // Ties the README to the release contract: the docs are the only place a
      // user learns the URL, and a versioned one silently breaks next release.
      const readme = fs.readFileSync(path.join(ROOT, "..", "README.md"), "utf8");
      assert.ok(
        /releases\/download\/windows-latest\/Deskpet-Setup\.exe/.test(readme),
        "README should link the stable windows-latest installer"
      );
      const versioned = readme.match(/releases\/download\/windows-latest\/Deskpet-[\d.]/);
      assert.strictEqual(
        versioned,
        null,
        `README links a versioned name through the rolling channel, which breaks after the next release: ${versioned}`
      );
    });

    it("builds only the declared architecture in the publishing workflow", () => {
      // release.yml is the workflow that actually publishes, and it kept all
      // three D3 regressions long after build-installer.yml was fixed: --arm64,
      // the arm64 VC++ redist, and `npm install --no-audit`. None would fail
      // loudly; all three would ship the wrong thing.
      const workflow = releaseWorkflow();
      const active = workflow
        .split("\n")
        .filter((line) => !/^\s*#/.test(line))
        .join("\n");
      assert.ok(
        !/--arm64/.test(active),
        "release.yml must not pass --arm64; package.json declares x64 only"
      );
      assert.ok(
        !/vc_redist\.arm64/.test(active),
        "release.yml must not download the arm64 redistributable for x64 users"
      );
      assert.ok(
        /electron-builder --win --x64/.test(active),
        "release.yml should build the x64 NSIS target it declares"
      );
      assert.ok(
        !/npm install --no-audit/.test(active),
        "release.yml should use `npm ci` so the lockfile is honoured"
      );
      assert.ok(
        !/vulkan/i.test(active),
        "D3 dropped the Vulkan backend; release.yml must not fetch or verify it"
      );
      assert.ok(
        !/Copy-Item[^\n]*win-x64[^\n]*win-arm64/.test(active),
        "do not relabel an x64 binary as arm64 - that is how the wrong arch shipped silently"
      );
    });

    it("verifies sidecar architecture from the PE header, not from Test-Path", () => {
      const workflow = releaseWorkflow();
      assert.ok(
        /assert-pe-arch\.ps1/.test(workflow),
        "release.yml should assert the packaged binaries' architecture"
      );
      assert.ok(
        !/Test-Path[^\n]*arm64/.test(workflow),
        "Test-Path cannot tell an x64 binary from an arm64 one"
      );
      const script = path.join(ROOT, "..", "minicpm-sidecar", "scripts", "assert-pe-arch.ps1");
      assert.ok(fs.existsSync(script), "assert-pe-arch.ps1 should exist");
      assert.ok(
        /0x8664/.test(fs.readFileSync(script, "utf8")),
        "the assertion should compare real IMAGE_FILE_MACHINE values"
      );
    });

    it("resolves product metadata repository and publish configuration", () => {
      const productMetadata = require("../src/product-metadata");
      assert.strictEqual(productMetadata.githubOwner, "harshil342");
      assert.strictEqual(productMetadata.githubRepo, "J.A.R.V.I.S-Desk-Pet");
      assert.deepStrictEqual(pkg.build.publish, [
        {
          provider: "github",
          owner: "harshil342",
          repo: "J.A.R.V.I.S-Desk-Pet",
        },
      ]);
    });
  });
});

function findWorkflowJobIndex(workflow, jobName) {
  const match = String(workflow || "").match(new RegExp(`(?:^|\\r?\\n)  ${jobName}:\\r?\\n`));
  return match ? match.index : -1;
}

function assertWorkflowOrder(workflow, fetchCommand, verifyCommand, buildCommand) {
  const fetchIndex = workflow.indexOf(fetchCommand);
  const verifyIndex = workflow.indexOf(verifyCommand);
  const buildIndex = workflow.indexOf(buildCommand);
  assert.ok(fetchIndex >= 0, `workflow should run: ${fetchCommand}`);
  assert.ok(verifyIndex >= 0, `workflow should run: ${verifyCommand}`);
  assert.ok(buildIndex >= 0, `workflow should run: ${buildCommand}`);
  assert.ok(fetchIndex < verifyIndex, `${fetchCommand} should run before ${verifyCommand}`);
  assert.ok(verifyIndex < buildIndex, `${verifyCommand} should run before ${buildCommand}`);
}

// Every relative path an npm script points at. `npm run lint` in CI failed with
// "Cannot find module .../scripts/check-syntax.js" because scripts/* is
// gitignored with an allowlist and the new file had no `!` line. It existed on
// disk, passed locally, and vanished on the runner. This makes that class of
// bug a test failure instead of a surprise in CI.
const SCRIPT_PATH_RE =
  /(?<=^|[\s"'=])(?:\.\.\/|\.\/)?[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+/g;

function referencedScriptPaths() {
  const found = new Map();
  for (const [name, command] of Object.entries(pkg.scripts || {})) {
    if (typeof command !== "string") continue;
    for (const match of command.matchAll(SCRIPT_PATH_RE)) {
      const rel = match[0].replace(/^\.\//, "");
      if (rel.startsWith("-") || rel.includes("://")) continue;
      found.set(rel, name);
    }
  }
  return found;
}

function git(args, opts = {}) {
  return execFileSync("git", args, {
    cwd: ROOT,
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    ...opts,
  });
}

describe("npm script targets", () => {
  const targets = referencedScriptPaths();

  it("found the script paths to check", () => {
    // Guards the guard: if the regex ever stops matching, both tests below go
    // vacuously green and this failure mode comes straight back.
    assert.ok(targets.size >= 30, `expected to parse many script paths, got ${targets.size}`);
    assert.ok(targets.has("scripts/check-syntax.js"));
    assert.ok(targets.has("../scripts/release.mjs"));
  });

  it("only references files that exist on disk", () => {
    const missing = [];
    for (const [rel, scriptName] of targets) {
      if (!fs.existsSync(path.resolve(ROOT, rel))) missing.push(`${scriptName} -> ${rel}`);
    }
    assert.deepStrictEqual(missing, [], `npm scripts point at missing files: ${missing.join(", ")}`);
  });

  it("only references files that git will actually ship", () => {
    let tracked;
    let toplevel;
    try {
      toplevel = git(["rev-parse", "--show-toplevel"]).trim();
      // Run from the root: git ls-files with no pathspec only lists files under
      // the current directory, which would silently skip the ../ scripts too.
      tracked = new Set(
        git(["ls-files", "-z", "--full-name"], { cwd: toplevel }).split("\0").filter(Boolean)
      );
      toplevel = git(["rev-parse", "--show-toplevel"]).trim();
    } catch {
      return; // Not a git work tree (e.g. a source tarball). Nothing to assert.
    }
    // Not ignored is not enough: an untracked file passes check-ignore and still
    // vanishes on the runner. Both halves matter.
    const unshipped = [];
    for (const [rel, scriptName] of targets) {
      // Git always speaks forward slashes, relative to the work tree root.
      const key = path
        .relative(toplevel, path.resolve(ROOT, rel))
        .split(path.sep)
        .join("/");
      if (!tracked.has(key)) unshipped.push(`${scriptName} -> ${rel} (${key})`);
    }
    assert.deepStrictEqual(
      unshipped,
      [],
      `these exist locally but are not committed, so they break in CI. git add them: ${unshipped.join(", ")}`
    );
  });
});
