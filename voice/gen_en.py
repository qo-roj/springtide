#!/srv/kokoro/.venv/bin/python
"""Generate the Springtide voice sets (kokoro, local). Themes (2026-11-05).

Run once on a build host — output ships to the phone and plays offline.
All fragments: 24000 Hz mono 16-bit WAV so the daemon can stitch them
with plain frame concatenation (no resampler, no deps on the phone).

Usage:
  gen_en.py                       # regenerate DEFAULT theme entirely
  gen_en.py seven                 # regenerate the 'seven' theme entirely
  gen_en.py kitt n7 n42           # regenerate ONLY n7,n42 in theme 'kitt'

Every fragment goes through normalize() — the loud chain. NEVER ship raw
kokoro output; loudness must be by construction (2026-11-05).
"""
import subprocess
import sys
from pathlib import Path

THEMES = {
    # default: warm, neutral assistant voice
    "en": {
        "voice": "af_heart",
        "sentences": {
            "test": "Particulate filter monitor. Voice check. All announcements are working.",
            "regen_start": "Particulate filter regeneration initiated. Please keep driving.",
            "p_finished": "Regeneration finished after",
            "minutes": "minutes",
            "and": "and",
            "km_end": "kilometers. The filter is clean.",
            "min_end": "minutes. The filter is clean.",
            "regen_interrupted": "Regeneration interrupted. It will resume automatically on the next drive.",
            "conn_up": "Connection established. Monitoring activated.",
            "armed": "Baseline established. Monitoring armed.",
            "dtc_intro": "Engine fault stored. Fault code",
            "dtc_cleared": "Fault memory cleared.",
            "dtc_clear_failed": "Fault memory could not be cleared.",
        },
    },
    # Seven of Nine (Voyager): clipped, Borg-precise, minimal
    "seven": {
        "voice": "af_kore",
        "sentences": {
            "test": "Springtide operational. Voice check complete.",
            "regen_start": "Regeneration initiated. Continue driving.",
            "p_finished": "Regeneration complete after",
            "minutes": "minutes",
            "and": "and",
            "km_end": "kilometers. Efficiency restored.",
            "min_end": "minutes. Efficiency restored.",
            "regen_interrupted": "Regeneration interrupted. It will resume.",
            "conn_up": "Connection established. Monitoring active.",
            "armed": "Baseline assimilated. Detection armed.",
            "dtc_intro": "Malfunction registered. Fault code",
            "dtc_cleared": "Fault memory purged.",
            "dtc_clear_failed": "Fault memory purge failed.",
        },
    },
    # KITT (Knight Rider): formal, addresses the driver, faintly wry
    "kitt": {
        "voice": "am_adam",
        "sentences": {
            "test": "This is Springtide. All voice systems are functioning normally.",
            "regen_start": "Michael, a regeneration cycle has begun. Please keep driving.",
            "p_finished": "The regeneration is complete, Michael. Duration:",
            "minutes": "minutes",
            "and": "and",
            "km_end": "kilometers. The filter is now clean.",
            "min_end": "minutes. The filter is now clean.",
            "regen_interrupted": "I am sorry, Michael. The regeneration was interrupted. It will resume on our next drive.",
            "conn_up": "All systems online, Michael. Monitoring engaged.",
            "armed": "Baseline established. I am watching the engine temperatures, Michael.",
            "dtc_intro": "Michael, an engine fault has been registered. Code",
            "dtc_cleared": "The fault memory has been erased, Michael.",
            "dtc_clear_failed": "I was unable to erase the fault memory, Michael.",
        },
    },
}


def en_num(n):
    ones = ["zero", "one", "two", "three", "four", "five", "six",
            "seven", "eight", "nine"]
    teens = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
             "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty",
            "seventy", "eighty", "ninety"]
    if n < 10:
        return ones[n]
    if n < 20:
        return teens[n - 10]
    if n < 100:
        t, r = divmod(n, 10)
        return tens[t] + ("-" + ones[r] if r else "")
    return "one hundred" + (" " + en_num(n - 100) if n > 100 else "")


args = sys.argv[1:]
theme = next((a for a in args if a in THEMES), "en")
only = [a for a in args if a != theme]
conf = THEMES[theme]

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / theme
RAW = BASE_DIR / f"raw_{theme}"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

items = dict(conf["sentences"])
for n in range(0, 151):
    items[f"n{n}"] = en_num(n)
# DTC spelling (2026-11-07): digits d0..d9 + letters p/c/b/u, per theme
for n in range(0, 10):
    items[f"d{n}"] = en_num(n)
for l in "pcbu":
    items[l] = l.upper()
if only:
    items = {k: v for k, v in items.items() if k in only}

import soundfile as sf
from kokoro_onnx import Kokoro

kokoro = Kokoro(model_path="/srv/kokoro/kokoro-v1.0.onnx",
                voices_path="/srv/kokoro/voices-v1.0.bin")

for name, text in items.items():
    audio, sr = kokoro.create(text, voice=conf["voice"], speed=1.0, lang="en-us")
    sf.write(RAW / f"{name}.wav", audio, sr)
    print(f"kokoro[{theme}/{conf['voice']}] {name}: {text}", flush=True)


def normalize(path):
    """Broadcast-loud voice chain (2026-11-05: in-car over diesel noise).
    10:1 compression at -24 dB with +18 dB makeup, hard-limited to -1.5 dBFS:
    lands ~-11 LUFS integrated vs ~-18.7 for raw kokoro output. Uniform
    across fragments so stitched numbers match sentence loudness."""
    import os as _os
    tmp = str(path) + ".norm.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-af", "acompressor=threshold=-24dB:ratio=10:attack=5:release=60:makeup=18dB,"
                "alimiter=limit=0.841",
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", tmp],
        check=True)
    _os.replace(tmp, str(path))


for name in items:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(RAW / f"{name}.wav"),
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
         str(OUT / f"{name}.wav")],
        check=True)
    normalize(OUT / f"{name}.wav")

print(f"generated {len(items)} fragments for theme '{theme}' "
      f"(voice {conf['voice']}) -> {OUT}")
