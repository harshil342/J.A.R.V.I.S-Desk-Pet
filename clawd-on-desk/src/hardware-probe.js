"use strict";

// ponytail: native platform query using os + child_process, no external npm packages
const os = require("os");
const { execFileSync } = require("child_process");

/**
 * Detects GPUs and dedicated VRAM across Windows, macOS, and Linux.
 */
function probeGpus() {
  const gpus = [];
  const platform = process.platform;

  if (platform === "win32") {
    try {
      // Query Windows CIM for video controllers
      const psCmd = "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM | ConvertTo-Json -Compress";
      const out = execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-Command", psCmd], {
        timeout: 2500,
        encoding: "utf8",
        windowsHide: true,
      });
      const parsed = JSON.parse(out.trim());
      const list = Array.isArray(parsed) ? parsed : [parsed];
      for (const item of list) {
        if (!item || !item.Name) continue;
        const rawBytes = Number(item.AdapterRAM) || 0;
        gpus.push({
          name: String(item.Name).trim(),
          vramBytes: rawBytes,
          isDiscrete: /nvidia|geforce|rtx|gtx|radeon\s*rx|discrete|dedicated/i.test(item.Name),
        });
      }
    } catch {
      // Fallback: nvidia-smi if available on Windows
      try {
        const out = execFileSync("nvidia-smi", ["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], {
          timeout: 1500,
          encoding: "utf8",
          windowsHide: true,
        });
        const [name, memMb] = out.trim().split(",");
        if (name && memMb) {
          gpus.push({
            name: name.trim(),
            vramBytes: parseInt(memMb.trim(), 10) * 1024 * 1024,
            isDiscrete: true,
          });
        }
      } catch {}
    }
  } else if (platform === "darwin") {
    // On macOS, unified memory is shared between CPU and GPU
    try {
      const isAppleSilicon = process.arch === "arm64";
      const totalRam = os.totalmem();
      let chip = "Apple Silicon";
      try {
        chip = execFileSync("/usr/sbin/sysctl", ["-n", "machdep.cpu.brand_string"], {
          timeout: 1000,
          encoding: "utf8",
        }).trim() || chip;
      } catch {}

      gpus.push({
        name: chip,
        // On Apple Silicon, unified memory allocates up to 75% to GPU
        vramBytes: isAppleSilicon ? Math.floor(totalRam * 0.75) : 1024 * 1024 * 1024,
        isDiscrete: isAppleSilicon,
      });
    } catch {}
  } else if (platform === "linux") {
    // Linux: try nvidia-smi first
    try {
      const out = execFileSync("nvidia-smi", ["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], {
        timeout: 1500,
        encoding: "utf8",
      });
      const [name, memMb] = out.trim().split(",");
      if (name && memMb) {
        gpus.push({
          name: name.trim(),
          vramBytes: parseInt(memMb.trim(), 10) * 1024 * 1024,
          isDiscrete: true,
        });
      }
    } catch {}
  }

  return gpus;
}

/**
 * Selects primary compute GPU from list, preferring discrete GPUs.
 */
function pickPrimaryGpu(gpus) {
  if (!gpus || gpus.length === 0) return null;
  // Prefer discrete GPU with highest VRAM
  const discrete = gpus.filter((g) => g.isDiscrete);
  if (discrete.length > 0) {
    discrete.sort((a, b) => b.vramBytes - a.vramBytes);
    return discrete[0];
  }
  // Otherwise pick GPU with highest VRAM
  const sorted = [...gpus].sort((a, b) => b.vramBytes - a.vramBytes);
  return sorted[0];
}

/**
 * Evaluates hardware and recommends the optimal companion recipe.
 *
 * Rules:
 * - VRAM < 2GB or Integrated GPU -> "eco-sentinel"
 * - 2GB <= VRAM < 6GB -> "jarvis-studio"
 * - VRAM >= 6GB or Apple Silicon >= 16GB RAM -> "deep-cognition"
 */
function evaluateHardware(gpu, totalRamBytes) {
  const ramGB = totalRamBytes / (1024 * 1024 * 1024);
  const vramBytes = gpu ? gpu.vramBytes : 0;
  const vramGB = vramBytes / (1024 * 1024 * 1024);
  const gpuName = gpu ? gpu.name : "Integrated / CPU";

  let recommendedRecipe = "eco-sentinel";
  let reason = "";

  if (vramBytes >= 6 * 1024 * 1024 * 1024 || (process.platform === "darwin" && ramGB >= 16)) {
    recommendedRecipe = "deep-cognition";
    reason = `Detected ${gpuName} (${vramGB.toFixed(1)} GB VRAM) — Configured for Deep Cognition (2B High Precision).`;
  } else if (vramBytes >= 2 * 1024 * 1024 * 1024 || (gpu && gpu.isDiscrete)) {
    recommendedRecipe = "jarvis-studio";
    reason = `Detected ${gpuName} (${vramGB.toFixed(1)} GB VRAM) — Configured for J.A.R.V.I.S. Studio (Balanced Q8_0 + Kokoro Voice).`;
  } else {
    recommendedRecipe = "eco-sentinel";
    reason = `Detected ${gpuName} — Configured for Eco-Sentinel (Sub-1GB Low VRAM + sanoTTS).`;
  }

  return {
    recommendedRecipe,
    reason,
    vramGB,
    ramGB,
    gpuName,
  };
}

/**
 * Main hardware probe entrypoint.
 */
function probeHardware() {
  const gpus = probeGpus();
  const primaryGpu = pickPrimaryGpu(gpus);
  const totalRam = os.totalmem();
  const evalResult = evaluateHardware(primaryGpu, totalRam);

  return {
    platform: process.platform,
    arch: process.arch,
    cpuModel: (os.cpus() && os.cpus()[0] && os.cpus()[0].model) || "CPU",
    cpuCores: os.cpus().length,
    totalRamBytes: totalRam,
    totalRamGB: Number((totalRam / (1024 * 1024 * 1024)).toFixed(1)),
    gpus,
    primaryGpu,
    ...evalResult,
  };
}

module.exports = {
  probeGpus,
  pickPrimaryGpu,
  evaluateHardware,
  probeHardware,
};
