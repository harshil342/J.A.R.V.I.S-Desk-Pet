"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert");
const {
  pickPrimaryGpu,
  evaluateHardware,
  probeHardware,
} = require("../src/hardware-probe");

describe("hardware-probe", () => {
  it("pickPrimaryGpu prefers discrete GPU over integrated", () => {
    const gpus = [
      { name: "Intel(R) UHD Graphics", vramBytes: 1024 * 1024 * 1024, isDiscrete: false },
      { name: "NVIDIA GeForce RTX 4050 Laptop GPU", vramBytes: 4 * 1024 * 1024 * 1024, isDiscrete: true },
    ];
    const picked = pickPrimaryGpu(gpus);
    assert.strictEqual(picked.name, "NVIDIA GeForce RTX 4050 Laptop GPU");
    assert.strictEqual(picked.isDiscrete, true);
  });

  it("evaluateHardware recommends eco-sentinel for low VRAM or integrated GPU", () => {
    const gpu = { name: "Intel Iris Xe", vramBytes: 1024 * 1024 * 1024, isDiscrete: false };
    const res = evaluateHardware(gpu, 16 * 1024 * 1024 * 1024);
    assert.strictEqual(res.recommendedRecipe, "eco-sentinel");
    assert.match(res.reason, /Eco-Sentinel/i);
  });

  it("evaluateHardware recommends jarvis-studio for 2GB-6GB discrete GPU", () => {
    const gpu = { name: "NVIDIA GeForce RTX 3050", vramBytes: 4 * 1024 * 1024 * 1024, isDiscrete: true };
    const res = evaluateHardware(gpu, 16 * 1024 * 1024 * 1024);
    assert.strictEqual(res.recommendedRecipe, "jarvis-studio");
    assert.match(res.reason, /J\.A\.R\.V\.I\.S\. Studio/i);
  });

  it("evaluateHardware recommends deep-cognition for >=6GB VRAM", () => {
    const gpu = { name: "NVIDIA GeForce RTX 4070", vramBytes: 8 * 1024 * 1024 * 1024, isDiscrete: true };
    const res = evaluateHardware(gpu, 32 * 1024 * 1024 * 1024);
    assert.strictEqual(res.recommendedRecipe, "deep-cognition");
    assert.match(res.reason, /Deep Cognition/i);
  });

  it("probeHardware returns complete diagnostic object on current machine", () => {
    const hw = probeHardware();
    assert.ok(hw.platform);
    assert.ok(hw.arch);
    assert.ok(hw.cpuCores > 0);
    assert.ok(hw.totalRamGB > 0);
    assert.ok(["eco-sentinel", "jarvis-studio", "deep-cognition"].includes(hw.recommendedRecipe));
    assert.ok(typeof hw.reason === "string" && hw.reason.length > 0);
  });
});
