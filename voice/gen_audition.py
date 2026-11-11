#!/srv/kokoro/.venv/bin/python
"""Audition snippets: the conn_up sentence in alternate voices per theme,
so the driver can pick a timbre without regenerating a full set."""
import subprocess
from pathlib import Path
import soundfile as sf
from kokoro_onnx import Kokoro

BASE = Path(__file__).resolve().parent
OUT = BASE / "_audition"
RAW = BASE / "raw_audition"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

CANDS = [
    ("seven_c1_aoede", "af_aoede"),
    ("seven_c2_kore", "af_kore"),
    ("seven_c3_sarah", "af_sarah"),
    ("seven_c4_nicole", "af_nicole"),
    ("seven_c5_sky", "af_sky"),
    ("seven_c6_bella", "af_bella"),
    ("kitt_c1_michael", "am_michael"),
    ("kitt_c2_onyx", "am_onyx"),
    ("kitt_c3_adam", "am_adam"),
    ("kitt_c4_puck", "am_puck"),
    ("kitt_c5_fenrir", "am_fenrir"),
    ("kitt_c6_george", "bm_george"),
    ("kitt_c7_daniel", "bm_daniel"),
    ("kitt_c8_fable", "bm_fable"),
    ("kitt_c9_lewis", "bm_lewis"),
]
TEXT = "Connection established. Monitoring activated."

k = Kokoro(model_path="/srv/kokoro/kokoro-v1.0.onnx",
           voices_path="/srv/kokoro/voices-v1.0.bin")
for name, voice in CANDS:
    audio, sr = k.create(TEXT, voice=voice, speed=1.0, lang="en-us")
    sf.write(RAW / f"{name}.wav", audio, sr)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(RAW / f"{name}.wav"),
         "-af", "acompressor=threshold=-24dB:ratio=10:attack=5:release=60:makeup=18dB,"
                "alimiter=limit=0.841",
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(OUT / f"{name}.wav")],
        check=True)
    print(f"audition {name} ({voice})", flush=True)
print("auditions done")
