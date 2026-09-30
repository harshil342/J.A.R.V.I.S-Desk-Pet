"""Builds a unified, dense J.A.R.V.I.S. voice soundboard using the tuned Kokoro neural clone.

Synthesizes high-frequency DeskPet responses with enriched chest resonance and
mastered dynamics (peak normalized + subtle warmth saturation) so all 52 clips
have 100% consistent timbre and play instantly with 0ms latency.
"""

import json
import os
import shutil
import time
from pathlib import Path
import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

BASE_DIR = Path(__file__).resolve().parent.parent
SOUNDBOARD_DIR = BASE_DIR / "models" / "jarvis_soundboard"
CLIPS_DIR = SOUNDBOARD_DIR / "clips"
CATALOG_FILE = SOUNDBOARD_DIR / "catalog.json"

MODEL_PATH = BASE_DIR / "models" / "kokoro" / "kokoro-v1.0.int8.onnx"
VOICES_PATH = BASE_DIR / "models" / "kokoro" / "voices-v1.0.bin"

SOUNDBOARD_ENTRIES = [
    # ── Greetings & Identity ──
    ("greeting", "Allow me to introduce myself. I am Jarvis."),
    ("greeting", "Allow me to introduce myself. I am Jarvis, your personal artificial intelligence assistant."),
    ("greeting", "Good morning, sir. All systems are fully operational."),
    ("greeting", "Good afternoon, sir. How may I assist you today?"),
    ("greeting", "Good evening, sir. Online and ready for your command."),
    ("greeting", "Welcome back, sir. Systems are online."),
    ("greeting", "Hello, sir. How can I help you today?"),
    ("greeting", "At your service, sir."),
    ("greeting", "Standing by for your instructions, sir."),

    # ── Confirmations & Acknowledgments ──
    ("confirmation", "Yes, sir."),
    ("confirmation", "Right away, sir."),
    ("confirmation", "Very good, sir."),
    ("confirmation", "Certainly, sir."),
    ("confirmation", "Working on that now, sir."),
    ("confirmation", "On it, sir."),
    ("confirmation", "Understood, sir."),
    ("confirmation", "Processing your request, sir."),

    # ── Task Completion ──
    ("completion", "Done, sir."),
    ("completion", "Task complete, sir."),
    ("completion", "Operation completed successfully, sir."),
    ("completion", "All items updated, sir."),
    ("completion", "Saved successfully, sir."),

    # ── Reminders & Timers ──
    ("reminder", "Your reminder has been set, sir."),
    ("reminder", "Here is your reminder, sir."),
    ("reminder", "Timer started, sir."),
    ("reminder", "Timer complete, sir."),
    ("reminder", "All pending reminders have been cancelled, sir."),
    ("reminder", "You have no active reminders at the moment, sir."),

    # ── To-Do & Memory ──
    ("todo", "Added to your to-do list, sir."),
    ("todo", "Marked as complete, sir."),
    ("todo", "Removed from your to-do list, sir."),
    ("todo", "Your to-do list has been cleared, sir."),
    ("todo", "Your to-do list is already empty, sir."),
    ("todo", "I have recorded that in your memory notes, sir."),
    ("todo", "I don't have any notes saved on that topic, sir."),

    # ── System Telemetry & Hardware ──
    ("telemetry", "All systems nominal and functioning within normal parameters, sir."),
    ("telemetry", "CPU and memory levels are stable and running efficiently."),
    ("telemetry", "Network connection is active and stable, sir."),
    ("telemetry", "We are currently operating offline in air-gapped mode, sir."),
    ("telemetry", "System diagnostics complete. No anomalies detected."),
    ("telemetry", "Workstation locked, sir."),

    # ── Autonomous Research & PDF Briefings ──
    ("research", "Initiating deep research and compiling an executive briefing for you now, sir."),
    ("research", "Executive PDF briefing generated and opened on your desktop, sir."),

    # ── Desktop Tools, Apps, Browser, Media ──
    ("tools", "Opening your web browser now, sir."),
    ("tools", "Searching the web for you now, sir."),
    ("tools", "Launching application now, sir."),
    ("tools", "Desktop screenshot captured and saved, sir."),
    ("tools", "Adjusting system volume now, sir."),
    ("tools", "Your clipboard is currently empty, sir."),

    # ── Safety & Refusals ──
    ("alert", "I am afraid I cannot do that, sir. Destructive operations are outside my remit."),
    ("alert", "I was unable to retrieve that information at the moment, sir."),
    ("alert", "Shutting down now. Have a productive day, sir.")
]


def clean_slug(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == " " else "" for c in text.lower())
    words = cleaned.split()[:5]
    return "_".join(words)


def main():
    print(f"[*] Loading Kokoro engine from {MODEL_PATH.name}...")
    t0 = time.time()
    kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    print(f"[+] Loaded in {time.time() - t0:.2f}s")

    # Build calibrated dense voice vector:
    # 50% George (Butler baseline) + 35% Daniel (Punch & clarity) + 15% Lewis (Chest warmth)
    vg = kokoro.get_voice_style("bm_george")
    vd = kokoro.get_voice_style("bm_daniel")
    vl = kokoro.get_voice_style("bm_lewis")

    dense_blend = 0.50 * vg + 0.35 * vd + 0.15 * vl
    target_norm = np.linalg.norm(vg)
    dense_blend = dense_blend * (target_norm / np.linalg.norm(dense_blend)) * 1.12

    if CLIPS_DIR.exists():
        shutil.rmtree(CLIPS_DIR)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    catalog = []
    total = len(SOUNDBOARD_ENTRIES)
    print(f"[*] Generating {total} dense J.A.R.V.I.S. soundboard clips on local device...\n")

    t_start = time.time()
    for idx, (category, text) in enumerate(SOUNDBOARD_ENTRIES):
        clip_id = f"clip_{idx+1:03d}"
        slug = clean_slug(text)
        filename = f"jarvis_{idx+1:03d}_{slug}.wav"
        dest = CLIPS_DIR / filename

        t_clip = time.time()
        # speed=0.96 for calm, deliberate, authoritative Bettany pacing
        samples, sr = kokoro.create(text, voice=dense_blend, speed=0.96, lang="en-gb")

        # Master dynamics: peak normalize + subtle saturation for dense chest presence
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = samples / peak
        mastered = np.tanh(samples * 1.25) / np.tanh(1.25) * 0.96

        sf.write(str(dest), mastered, sr, subtype="PCM_16")
        duration = round(len(mastered) / float(sr), 2)
        elapsed = time.time() - t_clip

        catalog.append({
            "id": clip_id,
            "filename": filename,
            "path": str(dest.relative_to(BASE_DIR)),
            "text": text,
            "category": category,
            "duration": duration,
            "model": "kokoro_dense_clone",
        })

        print(f"  [{idx+1:02d}/{total}] {elapsed:4.1f}s | {duration:4.1f}s audio -> {filename}")

    CATALOG_FILE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    total_time = time.time() - t_start
    print(f"\n[+] Completed {len(catalog)} clips in {total_time:.1f}s ({total_time/total:.1f}s/clip)!")
    print(f"[+] Catalog saved: {CATALOG_FILE}")


if __name__ == "__main__":
    main()
