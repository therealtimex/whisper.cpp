// Runtime probe for a whisper_addon.node prebuilt, run the way the app runs it:
// with Electron as Node (ELECTRON_RUN_AS_NODE=1).
//
//   ELECTRON_RUN_AS_NODE=1 <electron> runtime_probe.cjs <addon> <model> <wav16kMonoS16>
//
// It follows realtimex-ai-app's frontend/src/electron/transcriptionEngines/native.cjs:
//   require -> getBackendDevices -> initialize(...) -> transcribeFloat32Async (legacy path)
//   -> initializeSession -> transcribeFloat32Async({sessionId}) -> destroySession -> dispose
// and then lets the process exit on its own, so static destructors run (the shutdown
// check, realtimex-ai-app#3238). It prints one JSON line per step; the caller records
// the exit code and judges the run with check_runtime_probe.py.
//
// Written by the #3229 Dev (realtimex-ai-app #3229 cross-platform evidence);
// kept here so every release can be checked the same way.
"use strict";
const fs = require("fs");
const path = require("path");

const [addonPath, modelPath, wavPath] = process.argv.slice(2);
const t0 = Date.now();
const log = (step, data) => console.log(JSON.stringify({ step, ms: Date.now() - t0, ...data }));

function readWav16kMono(file) {
  const b = fs.readFileSync(file);
  let off = 12;
  let fmt = null;
  while (off + 8 <= b.length) {
    const id = b.toString("ascii", off, off + 4);
    const size = b.readUInt32LE(off + 4);
    if (id === "fmt ") fmt = { channels: b.readUInt16LE(off + 10), rate: b.readUInt32LE(off + 12), bits: b.readUInt16LE(off + 22) };
    if (id === "data") {
      if (!fmt || fmt.channels !== 1 || fmt.rate !== 16000 || fmt.bits !== 16) throw new Error("need 16 kHz mono s16 WAV");
      const n = size / 2;
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) out[i] = b.readInt16LE(off + 8 + i * 2) / 32768;
      return out;
    }
    off += 8 + size + (size % 2);
  }
  throw new Error("no data chunk");
}

function transcribe(addon, samples, opts) {
  return new Promise((resolve, reject) => {
    addon.transcribeFloat32Async(samples, { sampleRate: 16000, channels: 1, ...opts }, (err, result) =>
      err ? reject(err) : resolve(result));
  });
}

function textOf(result) {
  if (typeof result === "string") return result;
  if (Array.isArray(result)) return result.map(textOf).join(" ");
  if (result && Array.isArray(result.segments)) return result.segments.map((s) => s.text ?? s[2] ?? "").join("");
  if (result && typeof result.text === "string") return result.text;
  return JSON.stringify(result).slice(0, 400);
}

const pipeline = (extra) => ({
  modelPath, threads: 2, language: "auto",
  inputSampleRate: 16000, outputSampleRate: 16000, inputChannels: 1,
  vadEnergyThreshold: 0.01, vadAttackMs: 120, vadReleaseMs: 260,
  minSpeechMs: 250, minSegmentMs: 250, maxSegmentMs: 10000, preSpeechPadMs: 300, postSpeechPadMs: 400,
  ...extra,
});

(async () => {
  log("env", {
    platform: process.platform, arch: process.arch, node: process.versions.node,
    electron: process.versions.electron || null, napi: process.versions.napi,
  });
  const addon = require(path.resolve(addonPath));
  log("loaded", { exports: Object.keys(addon).sort() });
  try {
    log("devices", { devices: addon.getBackendDevices?.() ?? null });
  } catch (e) {
    log("devices-error", { message: String(e && e.message) });
  }
  const samples = readWav16kMono(wavPath);
  log("wav", { samples: samples.length, seconds: samples.length / 16000 });

  addon.initialize(pipeline({}));
  log("initialized", {});
  const r1 = await transcribe(addon, samples, {});
  log("transcribed-legacy", { text: textOf(r1).trim() });

  if (typeof addon.initializeSession === "function") {
    const s = await addon.initializeSession(pipeline({ sessionId: "runtime-probe" }));
    log("session-initialized", { result: s ?? null });
    const r2 = await transcribe(addon, samples, { sessionId: "runtime-probe" });
    log("transcribed-session", { text: textOf(r2).trim() });
    const d = await addon.destroySession("runtime-probe");
    log("session-destroyed", { result: d ?? null });
  }
  if (typeof addon.dispose === "function") {
    addon.dispose();
    log("disposed", {});
  }
  process.on("exit", (code) => log("exit", { code }));
  log("done", {});
})().catch((e) => {
  log("error", { message: String(e && (e.stack || e.message || e)) });
  process.exitCode = 1;
});
