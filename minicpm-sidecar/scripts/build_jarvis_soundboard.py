import os
import re
import json
from pathlib import Path
import soundfile as sf
import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_WAV = BASE_DIR.parent / "downloads" / "paul_bettany_raw.wav"
VTT_FILE = BASE_DIR.parent / "downloads" / "jarvis_sub.en.vtt"
OUTPUT_DIR = BASE_DIR / "models" / "jarvis_soundboard"
CLIPS_DIR = OUTPUT_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

def parse_time(ts_str: str) -> float:
    parts = ts_str.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

def extract_sentences(vtt_path: Path):
    content = vtt_path.read_text(encoding="utf-8")
    cue_blocks = content.split("\n\n")
    words_with_times = []

    for block in cue_blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) >= 2 and "-->" in lines[0]:
            time_line = lines[0]
            m = re.match(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})", time_line)
            if not m:
                continue
            c_start = m.group(1)
            for l in lines[1:]:
                if "<" in l and ">" in l:
                    first_match = re.match(r"^([^<]+)<", l)
                    if first_match:
                        w = first_match.group(1).strip()
                        if w:
                            words_with_times.append((parse_time(c_start), w))
                    tokens = re.findall(r"<(\d{2}:\d{2}:\d{2}\.\d{3})><c>\s*([^<]+)</c>", l)
                    for t_stamp, word in tokens:
                        if word:
                            words_with_times.append((parse_time(t_stamp), word.strip()))

    clean_words = []
    for t, w in words_with_times:
        if not clean_words or (t != clean_words[-1][0] and w != clean_words[-1][1]):
            clean_words.append((t, w))

    sentences = []
    curr_words = []
    curr_start = clean_words[0][0] if clean_words else 0.0

    for i, (t, w) in enumerate(clean_words):
        curr_words.append(w)
        is_end = w.endswith((".", "!", "?"))
        is_pause = (i + 1 < len(clean_words) and clean_words[i+1][0] - t > 0.75)
        if is_end or is_pause or len(curr_words) >= 12:
            en = clean_words[i+1][0] if i + 1 < len(clean_words) else t + 0.8
            text = " ".join(curr_words).strip()
            if len(text) >= 3:
                sentences.append((curr_start, en, text))
            if i + 1 < len(clean_words):
                curr_start = clean_words[i+1][0]
            curr_words = []

    return sentences

def categorize_text(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["introduce", "i am jarvis", "welcome", "hello", "good morning", "good evening", "good afternoon", "setup complete", "systems check", "protocols"]):
        return "greeting"
    if any(k in t for k in ["reminder", "requires your attention", "scheduled", "appointment", "calendar", "event"]):
        return "reminder"
    if any(k in t for k in ["very good", "right away", "certainly", "as you wish", "per your request", "acknowledged", "understood", "yes sir"]):
        return "confirmation"
    if any(k in t for k in ["complete", "completed", "upload", "ready", "finished", "compiled", "prepared"]):
        return "completion"
    if any(k in t for k in ["power", "battery", "vitals", "temperature", "network", "diagnostic", "radar", "threat", "armored", "suit"]):
        return "telemetry"
    if any(k in t for k in ["sorry", "afraid", "unable", "not possible", "error", "warning", "caution"]):
        return "alert"
    return "general"

def main():
    print(f"[Soundboard] Reading raw audio from {RAW_WAV}...")
    data, sr = sf.read(str(RAW_WAV))
    if data.ndim > 1:
        data = data.mean(axis=1)

    print(f"[Soundboard] Parsing exact word timestamps from {VTT_FILE}...")
    sentences = extract_sentences(VTT_FILE)
    print(f"[Soundboard] Extracted {len(sentences)} distinct Paul Bettany dialogue sentences.")

    catalog = []

    for idx, (st, en, text) in enumerate(sentences):
        # Generous boundary padding
        start_sec = max(0.0, st - 0.05)
        end_sec = min(len(data) / sr, en + 0.15)
        dur = end_sec - start_sec
        if dur < 0.4:
            continue

        start_samp = int(start_sec * sr)
        end_samp = int(end_sec * sr)
        audio_slice = data[start_samp:end_samp]

        peak = np.max(np.abs(audio_slice))
        if peak < 0.01:
            continue
        # Normalize to -0.5 dB
        audio_slice = audio_slice / peak * 0.95

        category = categorize_text(text)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", text[:35].lower()).strip("_")
        filename = f"jarvis_{idx+1:03d}_{slug}.wav"
        out_file = CLIPS_DIR / filename
        sf.write(str(out_file), audio_slice, sr)

        catalog.append({
            "id": f"clip_{idx+1:03d}",
            "filename": filename,
            "path": str(out_file.relative_to(BASE_DIR)),
            "text": text,
            "category": category,
            "duration": round(dur, 2),
            "start": round(start_sec, 2),
            "end": round(end_sec, 2),
        })

    catalog_path = OUTPUT_DIR / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"[Soundboard] Sliced {len(catalog)} audio clips into {CLIPS_DIR}")
    print(f"[Soundboard] Catalog saved to {catalog_path}")

    # Summary by category
    categories = {}
    for c in catalog:
        cat = c["category"]
        categories[cat] = categories.get(cat, 0) + 1
    print("[Soundboard] Category breakdown:", categories)

if __name__ == "__main__":
    main()
