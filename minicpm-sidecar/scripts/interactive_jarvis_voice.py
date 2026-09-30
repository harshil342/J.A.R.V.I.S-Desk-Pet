import os
import sys
import subprocess
from pathlib import Path
import soundfile as sf
from kokoro_onnx import Kokoro

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.int8.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"
AUDIO_DIR = Path(__file__).resolve().parent / "audio_samples"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

def play_audio(file_path: Path):
    print(f"[*] Playing {file_path.name}...")
    if sys.platform == "win32" and file_path.suffix.lower() == ".wav":
        cmd = f"(New-Object System.Media.SoundPlayer '{file_path}').PlaySync()"
        subprocess.run(["powershell", "-NoProfile", "-Command", cmd], check=False)
    else:
        # Fallback to default system player
        os.startfile(str(file_path))

SOUNDBOARD_DIR = Path(__file__).resolve().parent.parent / "models" / "jarvis_soundboard" / "clips"

def main():
    print("=" * 65)
    print("        [+] DENSE J.A.R.V.I.S. VOICE & SOUNDBOARD SUITE")
    print("=" * 65)
    print("1. [DENSE CLONE] 'Allow me to introduce myself. I am Jarvis.'")
    print("2. [DENSE CLONE] 'Good morning, sir. All systems are fully operational.'")
    print("3. [DENSE CLONE] 'Here is your reminder, sir.'")
    print("4. [DENSE CLONE] 'All systems nominal and functioning within normal parameters, sir.'")
    print("5. [SOUNDBOARD] Search all dense J.A.R.V.I.S. soundboard clips")
    print("6. [SYNTHESIS] Speak custom text via dense neural clone")
    print("0. Exit")
    print("-" * 65)

    samples = {
        "1": SOUNDBOARD_DIR / "jarvis_001_allow_me_to_introduce_myself.wav",
        "2": SOUNDBOARD_DIR / "jarvis_003_good_morning_sir_all_systems.wav",
        "3": SOUNDBOARD_DIR / "jarvis_024_here_is_your_reminder_sir.wav",
        "4": SOUNDBOARD_DIR / "jarvis_036_all_systems_nominal_and_functioning.wav",
    }

    if len(sys.argv) > 1:
        choice = sys.argv[1].strip()
        if choice in samples and samples[choice].exists():
            play_audio(samples[choice])
            return
        elif choice == "speak" and len(sys.argv) > 2:
            custom_text = " ".join(sys.argv[2:])
            speak_custom(custom_text)
            return

    kokoro = None
    dense_blend = None

    while True:
        try:
            choice = input("\nEnter choice [1-6, 0 to quit]: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0" or choice.lower() == "q":
            print("Exiting.")
            break

        if choice in samples:
            target = samples[choice]
            if target.exists():
                play_audio(target)
            else:
                print(f"File not found: {target}")
        elif choice == "5":
            from gateway import jarvis_soundboard
            cat = jarvis_soundboard.load_catalog(reload=True)
            term = input("Search query (e.g. 'alarm', 'reminder', 'status', 'introduce'): ").strip()
            matches = [c for c in cat if term.lower() in c["text"].lower()][:8]
            if not matches:
                print(f"No clips found containing '{term}'")
            else:
                for idx, m in enumerate(matches):
                    print(f"  [{idx+1}] \"{m['text']}\" ({m['duration']}s)")
                sub = input("Select clip to play [1-N, or enter to skip]: ").strip()
                if sub.isdigit() and 1 <= int(sub) <= len(matches):
                    target_clip = matches[int(sub) - 1]
                    jarvis_soundboard.play_audio_file(target_clip["path"])
        elif choice == "6":
            text = input("Enter text for J.A.R.V.I.S. to speak: ").strip()
            if not text:
                text = "Protocol initiated. Standing by for further instructions, sir."
            if kokoro is None:
                import numpy as np
                print("Loading Kokoro engine & dense voice vector...")
                kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
                vg = kokoro.get_voice_style("bm_george")
                vd = kokoro.get_voice_style("bm_daniel")
                vl = kokoro.get_voice_style("bm_lewis")
                dense_blend = 0.50 * vg + 0.35 * vd + 0.15 * vl
                target_norm = np.linalg.norm(vg)
                dense_blend = dense_blend * (target_norm / np.linalg.norm(dense_blend)) * 1.12
            
            import numpy as np
            print(f"Synthesizing (dense clone): \"{text}\"")
            samples_data, sample_rate = kokoro.create(text, voice=dense_blend, speed=0.96, lang="en-gb")
            peak = np.max(np.abs(samples_data))
            if peak > 0:
                samples_data = samples_data / peak
            mastered = np.tanh(samples_data * 1.25) / np.tanh(1.25) * 0.96
            temp_file = AUDIO_DIR / "jarvis_custom.wav"
            sf.write(str(temp_file), mastered, sample_rate, subtype="PCM_16")
            play_audio(temp_file)
        else:
            print("Invalid choice.")

def speak_custom(text: str):
    import numpy as np
    print(f"[Kokoro] Synthesizing dense custom text: \"{text}\"")
    kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    vg = kokoro.get_voice_style("bm_george")
    vd = kokoro.get_voice_style("bm_daniel")
    vl = kokoro.get_voice_style("bm_lewis")
    dense_blend = 0.50 * vg + 0.35 * vd + 0.15 * vl
    target_norm = np.linalg.norm(vg)
    dense_blend = dense_blend * (target_norm / np.linalg.norm(dense_blend)) * 1.12
    samples_data, sample_rate = kokoro.create(text, voice=dense_blend, speed=0.96, lang="en-gb")
    peak = np.max(np.abs(samples_data))
    if peak > 0:
        samples_data = samples_data / peak
    mastered = np.tanh(samples_data * 1.25) / np.tanh(1.25) * 0.96
    temp_file = AUDIO_DIR / "jarvis_custom.wav"
    sf.write(str(temp_file), mastered, sample_rate, subtype="PCM_16")
    play_audio(temp_file)

if __name__ == "__main__":
    main()
