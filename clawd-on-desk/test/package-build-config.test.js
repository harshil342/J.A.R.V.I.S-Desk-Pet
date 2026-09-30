const assert = require("node:assert");
const { describe, it } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { minimatch } = require("minimatch");

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

  describe("GitHub release publication", () => {
    it("publishes GitHub releases only for version tags", () => {
      const workflow = fs.readFileSync(path.join(ROOT, ".github", "workflows", "build.yml"), "utf8");
      const releaseIndex = findWorkflowJobIndex(workflow, "release");
      assert.ok(releaseIndex >= 0, "workflow should define a release job");
      const releaseGateIndex = workflow.indexOf("if: startsWith(github.ref, 'refs/tags/v')", releaseIndex);
      const bodyPathIndex = workflow.indexOf("body_path: docs/releases/release-${{ github.ref_name }}.md", releaseIndex);
      assert.ok(releaseGateIndex >= 0, "release job should be gated to v* tags");
      assert.ok(bodyPathIndex >= 0, "release job should still use tag-specific release notes");
      assert.ok(releaseGateIndex < bodyPathIndex, "release job gate should run before release publication");
    });

    it("creates tag releases as drafts for final asset inspection", () => {
      const workflow = fs.readFileSync(path.join(ROOT, ".github", "workflows", "build.yml"), "utf8");
      const releaseIndex = findWorkflowJobIndex(workflow, "release");
      assert.ok(releaseIndex >= 0, "workflow should define a release job");
      const actionIndex = workflow.indexOf("softprops/action-gh-release@v2", releaseIndex);
      const draftIndex = workflow.indexOf("draft: true", actionIndex);
      const prereleaseIndex = workflow.indexOf("prerelease: ${{ contains(github.ref_name, '-') }}", actionIndex);
      assert.ok(actionIndex >= 0, "release job should use the GitHub release action");
      assert.ok(draftIndex > actionIndex, "tag releases should be created as drafts first");
      assert.ok(prereleaseIndex > actionIndex, "hyphenated tags should be marked prerelease");
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
