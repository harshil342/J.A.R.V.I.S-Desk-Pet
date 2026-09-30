#!/usr/bin/env python3
"""Generate the pet's default cue clips.

These are functional tones, not a sound design: short, percussive, and
distinguishable from each other at a glance. They exist so the pet is audible
out of the box without shipping audio of unclear provenance. Replace them with
designed sounds by dropping files into a theme's `sounds/` folder and pointing
`voice.json` at them — nothing here is load-bearing.

Standard library only. Output is 16-bit mono WAV at 44.1 kHz.
"""
from __future__ import annotations

import argparse
import math
import struct
import wave
from pathlib import Path

RATE = 44100
PEAK = 0.45  # the engine scales to 0.12; this is headroom, not loudness


def _env(i: int, n: int, attack: float = 0.006, decay: float = 0.55) -> float:
    """Percussive envelope: fast attack, exponential decay, hard zero at the end.

    The tail matters. A tone that is still ringing when it stops is a click, and
    a pet that clicks is a pet people mute.
    """
    t = i / RATE
    total = n / RATE
    if t < attack:
        a = t / attack
    else:
        a = math.exp(-(t - attack) / decay)
    fade = min(1.0, (total - t) / 0.012)  # last 12 ms ramps to silence
    return a * max(0.0, fade)


def tone(freq: float, dur: float, harmonics=(1.0, 0.28, 0.09), detune: float = 0.0) -> list[float]:
    n = int(RATE * dur)
    out = []
    for i in range(n):
        t = i / RATE
        v = 0.0
        for k, amp in enumerate(harmonics, start=1):
            v += amp * math.sin(2 * math.pi * freq * k * t)
        if detune:
            v += 0.4 * math.sin(2 * math.pi * freq * (1 + detune) * t)
        out.append(v * _env(i, n))
    return out


def mix(*layers: list[float]) -> list[float]:
    n = max(len(x) for x in layers)
    out = [0.0] * n
    for layer in layers:
        for i, v in enumerate(layer):
            out[i] += v
    return out


def silence(dur: float) -> list[float]:
    return [0.0] * int(RATE * dur)


def seq(*parts: list[float]) -> list[float]:
    out: list[float] = []
    for p in parts:
        out.extend(p)
    return out


def normalize(samples: list[float]) -> list[float]:
    peak = max((abs(s) for s in samples), default=0.0)
    if peak < 1e-9:
        return samples
    k = PEAK / peak
    return [s * k for s in samples]


def write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(
        struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in normalize(samples)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)


# Each cue is one idea, stated in under 400ms. A rising pair reads as "done",
# a falling pair as "something went wrong", a doubled high note as "answer me".
CUES = {
    # Needs a human now. Two identical high pings so it cuts through a bed.
    "approval": seq(tone(1174.7, 0.11), silence(0.05), tone(1174.7, 0.16)),
    # A falling minor third. Reads as wrong without being alarming.
    "error": seq(tone(392.0, 0.13), tone(311.1, 0.22)),
    # Low, soft, single. The sound of "I am busy", not "look at me".
    "working": tone(261.6, 0.17, harmonics=(1.0, 0.18)),
    # A fifth above working, so the two are obviously the same animal.
    "thinking": tone(392.0, 0.15, harmonics=(1.0, 0.18)),
    # Rising fourth. The only cue that goes up and lands.
    "finished": seq(tone(523.3, 0.10), tone(698.5, 0.20)),
    # Barely there. Longer and lower than anything else.
    "sleeping": tone(174.6, 0.38, harmonics=(1.0, 0.10), detune=0.004),
    # A slow lift, the inverse of falling asleep.
    "wake": tone(261.6, 0.26, harmonics=(1.0, 0.14)),
    # Triple knock. Distinct from every pitched cue, so it is recognisable with
    # no sight of the pet.
    "attention": seq(tone(880.0, 0.05), silence(0.04), tone(880.0, 0.05), silence(0.04), tone(880.0, 0.12)),
    "idle": tone(329.6, 0.09, harmonics=(1.0, 0.12)),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory to write the .wav files into")
    ap.add_argument("--sample-rate", type=int, default=RATE)
    args = ap.parse_args()

    out = Path(args.out)
    written = 0
    for name, samples in CUES.items():
        path = out / f"{name}.wav"
        write_wav(path, samples)
        written += 1
        print(f"  {name:<10} {len(samples) / RATE * 1000:5.0f} ms  {path.name}")
    print(f"wrote {written} clips to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
