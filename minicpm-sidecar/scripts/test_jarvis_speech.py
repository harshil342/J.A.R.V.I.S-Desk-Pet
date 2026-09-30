import os
from pathlib import Path
import soundfile as sf
import numpy as np
from kokoro_onnx import Kokoro

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.int8.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

OUTPUT_DIR = Path(__file__).resolve().parent / "audio_samples"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

print("[Kokoro] Loading ONNX model...")
kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))

available_voices = kokoro.get_voices()
print(f"[Kokoro] Available voices ({len(available_voices)}):")
british_voices = [v for v in available_voices if v.startswith("bm_") or v.startswith("bf_")]
print("British voices:", british_voices)

prompt = "Good evening, sir. J.A.R.V.I.S. is online and all systems are fully operational."

# 1. Generate with bm_george (Classic British Butler)
if "bm_george" in available_voices:
    print("\n[1/3] Synthesizing speech with bm_george (British Butler)...")
    samples, sample_rate = kokoro.create(prompt, voice="bm_george", speed=1.0, lang="en-gb")
    out_george = OUTPUT_DIR / "jarvis_kokoro_george.wav"
    sf.write(str(out_george), samples, sample_rate)
    print(f"Saved: {out_george} ({len(samples)/sample_rate:.2f}s)")

# 2. Generate with bm_fable (Smooth British)
if "bm_fable" in available_voices:
    print("\n[2/3] Synthesizing speech with bm_fable (Smooth British)...")
    samples, sample_rate = kokoro.create(prompt, voice="bm_fable", speed=1.0, lang="en-gb")
    out_fable = OUTPUT_DIR / "jarvis_kokoro_fable.wav"
    sf.write(str(out_fable), samples, sample_rate)
    print(f"Saved: {out_fable} ({len(samples)/sample_rate:.2f}s)")

# 3. Voice Blending: 70% George + 30% Fable
if "bm_george" in available_voices and "bm_fable" in available_voices:
    print("\n[3/3] Blending 70% George + 30% Fable for Paul Bettany cadence...")
    # Check if get_voice_style or voice blending is supported directly
    try:
        # kokoro_onnx stores voices in kokoro.voices or voice style array
        v_george = kokoro.get_voice_style("bm_george")
        v_fable = kokoro.get_voice_style("bm_fable")
        v_blend = 0.70 * v_george + 0.30 * v_fable
        samples, sample_rate = kokoro.create(prompt, voice=v_blend, speed=1.0, lang="en-gb")
        out_blend = OUTPUT_DIR / "jarvis_kokoro_blend.wav"
        sf.write(str(out_blend), samples, sample_rate)
        print(f"Saved: {out_blend} ({len(samples)/sample_rate:.2f}s)")
    except Exception as e:
        print(f"Custom blend note: {e}")

print("\nAll audio generation complete!")
