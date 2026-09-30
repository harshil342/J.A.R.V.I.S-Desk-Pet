import os
import sys
import httpx
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kokoro"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.int8.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin"

MODEL_PATH = MODEL_DIR / "kokoro-v1.0.int8.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

def download_file(url: str, dest: Path, desc: str):
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"[Kokoro] {desc} already exists ({dest.stat().st_size / 1024 / 1024:.1f} MB). Skipping.")
        return
    print(f"[Kokoro] Downloading {desc} from {url}...")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = (downloaded / total) * 100
                        print(f"\r[Kokoro] {desc}: {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB ({pct:.0f}%)", end="", flush=True)
    print(f"\n[Kokoro] Finished downloading {desc} -> {dest}")

if __name__ == "__main__":
    download_file(MODEL_URL, MODEL_PATH, "kokoro-v1.0.int8.onnx (~88 MB)")
    download_file(VOICES_URL, VOICES_PATH, "voices-v1.0.bin (~28 MB)")
    print("[Kokoro] Model and voice assets ready.")
