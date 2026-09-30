"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const downloader = require("../src/minicpm-model-download");

describe("minicpm-model-download", () => {
  it("declares the known Q8_0 GGUF size as a progress fallback", () => {
    assert.equal(downloader.MODEL_SIZE_BYTES, 1_153_529_216);
  });

  it("parses Cloudflare trace country", () => {
    assert.equal(downloader.parseCloudflareTrace("ip=1.2.3.4\nloc=CN\nwarp=off\n"), "CN");
    assert.equal(downloader.parseCloudflareTrace("loc=us\n"), "US");
    assert.equal(downloader.parseCloudflareTrace("ip=1.2.3.4\n"), null);
  });

  it("routes China IPs to ModelScope and other regions to Hugging Face", () => {
    assert.equal(downloader.selectProviderForCountry("CN"), "modelscope");
    assert.equal(downloader.selectProviderForCountry("US"), "huggingface");
    assert.equal(downloader.selectProviderForCountry(null), "modelscope");
  });

  it("supports explicit provider overrides", () => {
    assert.equal(downloader.normalizeProvider("hf"), "huggingface");
    assert.equal(downloader.normalizeProvider("hugging-face"), "huggingface");
    assert.equal(downloader.normalizeProvider("ms"), "modelscope");
    assert.equal(downloader.normalizeProvider("model-scope"), "modelscope");
    assert.equal(downloader.normalizeProvider("unknown"), null);
  });

  it("adds provider tokens only to first-party hosts", () => {
    const hfHeaders = downloader.buildHeaders(
      "huggingface",
      "https://huggingface.co/openbmb/repo/resolve/main/model.gguf",
      { HF_TOKEN: "hf_test" }
    );
    assert.equal(hfHeaders.authorization, "Bearer hf_test");

    const hfCdnHeaders = downloader.buildHeaders(
      "huggingface",
      "https://cdn-lfs.huggingface.co/object",
      { HF_TOKEN: "hf_test" }
    );
    assert.equal(hfCdnHeaders.authorization, undefined);

    const msHeaders = downloader.buildHeaders(
      "modelscope",
      "https://modelscope.cn/models/OpenBMB/repo/resolve/master/model.gguf",
      { MODELSCOPE_API_TOKEN: "ms_test" }
    );
    assert.equal(msHeaders.authorization, "Bearer ms_test");
    assert.match(msHeaders["user-agent"], /modelscope\//);
  });

  it("builds ModelScope snapshot counting headers", () => {
    const headers = downloader.buildModelScopeSnapshotHeaders({
      MODELSCOPE_API_TOKEN: "ms_test",
      MINICPM_MODELSCOPE_SESSION_ID: "fixed-session",
    });

    assert.equal(headers.Snapshot, "True");
    assert.equal(headers.authorization, "Bearer ms_test");
    assert.match(headers["user-agent"], /modelscope\//);
    assert.match(headers["user-agent"], /session_id\/fixed-session/);
    assert.equal(typeof headers["snapshot-identifier"], "string");
    assert.equal(typeof headers["X-Request-ID"], "string");
  });

  it("falls back to the other provider unless forced", () => {
    assert.deepEqual(downloader.providerOrder("modelscope", false), ["modelscope", "huggingface"]);
    assert.deepEqual(downloader.providerOrder("huggingface", false), ["huggingface", "modelscope"]);
    assert.deepEqual(downloader.providerOrder("modelscope", true), ["modelscope"]);
  });

  it("provides model presets for MiniCPM5-1B and MiniCPM5-2B", () => {
    assert.ok(downloader.MODEL_PRESETS["minicpm5-1b"]);
    assert.ok(downloader.MODEL_PRESETS["minicpm5-2b"]);
    assert.ok(downloader.MODEL_PRESETS["eco-sentinel"]);
    assert.ok(downloader.MODEL_PRESETS["jarvis-studio"]);
    assert.ok(downloader.MODEL_PRESETS["deep-cognition"]);
    assert.equal(downloader.MODEL_PRESETS["minicpm5-1b"].filename, "MiniCPM5-1B-Q8_0.gguf");
    assert.equal(downloader.MODEL_PRESETS["minicpm5-2b"].filename, "MiniCPM5-2B-Q4_K_M.gguf");
    assert.equal(downloader.MODEL_PRESETS["minicpm5-1b"].sizeBytes, 1_153_529_216);
    assert.equal(downloader.MODEL_PRESETS["minicpm5-2b"].sizeBytes, 1_561_318_368);
  });

  it("resolves model presets with fallback to default", () => {
    assert.equal(downloader.getModelPreset("minicpm5-1b").id, "minicpm5-1b");
    assert.equal(downloader.getModelPreset("minicpm5-2b").id, "minicpm5-2b");
    assert.equal(downloader.getModelPreset("eco-sentinel").id, "eco-sentinel");
    assert.equal(downloader.getModelPreset("jarvis-studio").id, "jarvis-studio");
    assert.equal(downloader.getModelPreset("deep-cognition").id, "deep-cognition");
    assert.equal(downloader.getModelPreset("auto-detect").id, "jarvis-studio");
    assert.equal(downloader.getModelPreset("unknown").id, "minicpm5-1b");
    assert.equal(downloader.getModelPreset(null).id, "minicpm5-1b");
  });

  it("builds provider URLs for both presets", () => {
    const p1b = downloader.getProvidersForPreset("minicpm5-1b");
    assert.match(p1b.huggingface.url, /MiniCPM5-1B-Q8_0\.gguf/);
    assert.match(p1b.modelscope.url, /MiniCPM5-1B-Q8_0\.gguf/);

    const p2b = downloader.getProvidersForPreset("minicpm5-2b");
    assert.match(p2b.huggingface.url, /MiniCPM5-2B-Q4_K_M\.gguf/);
    assert.match(p2b.modelscope.url, /MiniCPM5-2B-Q4_K_M\.gguf/);
  });

  it("hashes files with sha256 without loading them fully", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "deskpet-hash-"));
    const file = path.join(dir, "probe.bin");
    fs.writeFileSync(file, "deskpet-model-bytes");
    const expected = crypto.createHash("sha256").update("deskpet-model-bytes").digest("hex");
    assert.equal(await downloader.sha256File(file), expected);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("reads the expected LFS hash from the Hugging Face API", async () => {
    const api = async () => JSON.stringify({ siblings: [
      { rfilename: "other.gguf", lfs: { oid: "sha256:" + "0".repeat(64) } },
      { rfilename: "MiniCPM5-1B-Q8_0.gguf", lfs: { oid: "sha256:" + "a".repeat(64) } },
    ] });
    assert.equal(
      await downloader.fetchHuggingFaceLfsSha({ repo: "openbmb/x", filename: "MiniCPM5-1B-Q8_0.gguf", requestTextImpl: api }),
      "a".repeat(64)
    );
  });

  it("returns null when the LFS hash is missing or unparsable", async () => {
    assert.equal(await downloader.fetchHuggingFaceLfsSha({
      repo: "x", filename: "y", requestTextImpl: async () => JSON.stringify({ siblings: [] }),
    }), null);
    assert.equal(await downloader.fetchHuggingFaceLfsSha({
      repo: "x", filename: "y", requestTextImpl: async () => "not-json{{{",
    }), null);
    assert.equal(await downloader.fetchHuggingFaceLfsSha({
      repo: "x", filename: "y", requestTextImpl: async () => { throw new Error("offline"); },
    }), null);
  });

  it("deletes the file and throws on sha256 mismatch", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "deskpet-badmodel-"));
    const api = async () => JSON.stringify({ siblings: [
      { rfilename: "MiniCPM5-1B-Q8_0.gguf", lfs: { oid: "b".repeat(64) } },
    ] });
    const downloadImpl = async ({ destination }) => {
      fs.writeFileSync(destination, "corrupt-bytes");
      return { ok: true, bytes: 13, path: destination };
    };
    await assert.rejects(
      downloader.downloadMiniCpmModel({
        modelPreset: "minicpm5-1b",
        destinationDir: dir,
        env: { MINICPM_MODEL_PROVIDER: "huggingface" },
        requestTextImpl: api,
        downloadImpl,
        snapshotCountImpl: null,
      }),
      /sha256 mismatch/
    );
    assert.equal(fs.existsSync(path.join(dir, "MiniCPM5-1B-Q8_0.gguf")), false);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("skips verification when the hash API is unreachable", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "deskpet-skiphash-"));
    const downloadImpl = async ({ destination }) => {
      fs.writeFileSync(destination, "unverified-bytes");
      return { ok: true, bytes: 16, path: destination };
    };
    const result = await downloader.downloadMiniCpmModel({
      modelPreset: "minicpm5-1b",
      destinationDir: dir,
      env: { MINICPM_MODEL_PROVIDER: "huggingface" },
      requestTextImpl: async () => { throw new Error("offline"); },
      downloadImpl,
      snapshotCountImpl: null,
    });
    assert.equal(result.ok, true);
    assert.equal(fs.existsSync(path.join(dir, "MiniCPM5-1B-Q8_0.gguf")), true);
    fs.rmSync(dir, { recursive: true, force: true });
  });
});
