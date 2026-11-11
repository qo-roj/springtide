#!/data/data/com.termux/files/usr/bin/python3
"""Springtide — DPF coolant-mode daemon (Seat Ibiza 6L 1.4 TDI BMS).

Reads coolant (mode 01 PID 05), speed (0D) and RPM (0C) through the OBD
bridge (127.0.0.1:35000 -> BLE -> ELM327 clone -> K-line). Uses the
legislated KWP session via ATSP 0 AUTO init (this clone's explicit ATSP 5
is broken — live-verified 2026-11-02).

READ-MOSTLY BY DESIGN: the only autonomous traffic to the car is mode-01
requests (0100/0105/010D/010C, plus 0105E fuel-rate when the ECU supports
it), mode-03 stored-DTC reads, and AT commands. The ONE write path is the
DRIVER-COMMANDED mode-04 fault-memory clear — requested via dash.py ('c')
or --clear-dtc, never initiated by the daemon itself. (kwp_probe.py is the
separate diagnostic tool that deliberately sent session services to derive
the protocol verdicts.)

Detection (the driver's observed rule, corrected 2026-11-04 after the first live drive): a
regeneration shows coolant at 92-93 C — up to 90 C is normal operating
temperature and 91 C can occur normally; 92+ is the regen signal. The old
adaptive baseline+delta form produced a 45-min false positive on drive #1
(baseline locked at 81 while parked, normal 89-91 plateau then "exceeded" it).
  candidate = coolant >= regen_thresh_c (92 C, ABSOLUTE — not baseline-relative)
  confirmed = sustained >= confirm_s        -> alert "regeneration started"
  end       = coolant < regen_thresh_c for clear_s
              -> alert with duration, est. km, peak
  The 1 C sensor granularity is its own hysteresis; confirm_s/clear_s dwell
  times prevent flapping at the boundary.
  baseline  = rolling median of the last baseline_s of WARM samples — still
              used for ARMING (baseline_min_samples) and cold-engine reset,
              but no longer part of the regen math. During candidate/regen
              the baseline is FROZEN at candidate entry (log stability and
              restart resume).
  ECU lost mid-regen                       -> alert "interrupted"
  Cold engine (< warm_c)                   -> episode + samples + baseline
              cleared: every drive starts with a fresh baseline (prevents
              a cooling engine's 75-79 C samples faking a warm window).

Alerts are edge-triggered (one per transition). PRIMARY channel: in-car
VOICE — pre-generated WAV fragments in ~/dpf/voice/en/ (24000 Hz mono
s16, all identical format), dynamic numbers stitched at playback time.
Fully offline: no network, no third-party bot identities (2026-11-03: the
borrowed Telegram token was removed from the phone).
Telegram is OPTIONAL and OFF by default; enable via config
("telegram": {"enabled": true}) + ~/dpf/.env — failed sends then queue in
state.json and retry, never dropped silently.

CSV:   ~/dpf/logs/springtide_YYYYMMDD.csv
State: ~/dpf/state.json
Live:  ~/dpf/live.json (one compact dict per loop, for dash.py)
Config:~/dpf/config.json (all tunables)
"""
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASE = os.path.expanduser("~/dpf")
ENV_PATH = os.path.join(BASE, ".env")
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LIVE_PATH = os.path.join(BASE, "live.json")
LOG_DIR = os.path.join(BASE, "logs")
CMD_PATH = os.path.join(BASE, "cmd")      # driver command inbox (dash/CLI)
CLEAR_DTC_CMD = os.path.join(CMD_PATH, "clear_dtc")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 35000,
    "poll_s": 5.0,               # seconds between samples while ECU answers
    "ecu_miss_limit": 3,         # consecutive bad reads before declaring ECU lost
    "offline_probe_s": 30.0,     # re-probe cadence when ECU/bridge absent
    "elm_proto": "4",            # explicit ATSP init (clone auto-search broke 2026-11-04)
    "warm_c": 80.0,              # below this: engine considered cold, detection idle
    "baseline_s": 600.0,         # rolling window for the plateau median
    "baseline_min_samples": 40,  # don't arm detection before this many warm samples
    "regen_thresh_c": 92.0,       # coolant >= this => candidate (ABSOLUTE;
                                  #  up to 91 C is normal on this engine)
    "confirm_s": 180.0,          # candidate age at entry that confirms regen
    "cand_grace_s": 120.0,       # candidate survives dips shorter than this
                                 # (ride-2 2026-11-05 false negative: the regen
                                 #  signal sits AT the threshold and jitters
                                 #  91/92/93 with 1 C sensor granularity;
                                 #  equal to clear_s so confirm and end rules
                                 #  have symmetric jitter tolerance)
    "clear_s": 120.0,
    "max_regen_s": 5400.0,       # safety cap: force-end episode after 90 min
    "heartbeat_s": 1800.0,       # periodic status line in daemon.log
    "dtc_poll_s": 60.0,          # poll mode-03 stored DTCs every N s (0 = off)
    "voice": {"enabled": True, "lang": "en", "volume": 14},
    "telegram": {"enabled": False},
}


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    # Best-effort persistence: a failed save must not crash the daemon
    # (review 2026-11-02) — the supervisor restart covers persistent failure.
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log(f"WARNING: state save failed: {e}")


class Alerter:
    """OPTIONAL remote channel (Telegram). Off by default — the in-car
    voice is primary. Only constructed when config enables it, so no .env
    is ever touched or required."""

    def __init__(self):
        self.token = None
        self.chat = None
        try:
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        self.token = line.split("=", 1)[1].strip().strip("\"'")
                    elif line.startswith("TELEGRAM_CHAT_ID="):
                        self.chat = line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
        if self.token and ":" not in self.token:
            log("WARNING: token looks mangled (no ':') — alerts will fail")
        if not self.token or not self.chat:
            log("WARNING: Telegram not configured (.env) — alerts queued only")

    def send(self, text):
        if not self.token or not self.chat:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                return r.status == 200
        except Exception as e:
            log(f"telegram send failed: {e}")
            return False


# Latest cabin state for dynamic volume (2026-11-05): the poll loop
# refreshes it, speak() reads it. Single-threaded by design.
CURRENT = {"speed": 0.0}


class Announcer:
    """In-car voice channel — pre-generated WAV fragments, offline by design.

    All fragments are normalized 24000 Hz mono 16-bit PCM (voice/gen_en.py),
    so stitching is plain frame concatenation with short
    silence gaps: no resampling, no extra deps on the phone.
    Playback: termux-volume (media volume) + termux-media-player (fire and
    forget — events are minutes apart, no overlap concern).
    """

    GAP_S = 0.16
    MAX_N = 150  # number fragments generated 0..150 (90-min cap, km safety)

    def __init__(self, cfg):
        v = cfg.get("voice") or {}
        self.enabled = bool(v.get("enabled", True))
        self.lang = v.get("lang", "en")
        # theme = fragment set under voice/<theme>/ (2026-11-05):
        # "en" (default af_heart), "seven" (Seven of Nine), "kitt" (KITT)
        self.theme = v.get("theme") or self.lang
        self.volume = int(v.get("volume", 15))
        self.dir = os.path.join(BASE, "voice", self.theme)
        # dynamic loudness (2026-11-05): scale media volume with cabin
        # speed — idle needs far less than 90+ km/h of wind and road noise
        self.dyn = bool(v.get("dynamic_volume", False))
        self.vol_idle = float(v.get("vol_idle", 9))
        self.vol_road = float(v.get("vol_road", 15))
        self.road_km_h = max(1.0, float(v.get("road_km_h", 90)))
        if self.enabled and not os.path.isfile(os.path.join(self.dir, "regen_start.wav")):
            log(f"WARNING: voice fragments missing in {self.dir} — announcements muted")
            self.enabled = False

    def _path(self, name):
        p = os.path.join(self.dir, f"{name}.wav")
        return p if os.path.isfile(p) else None

    def speak(self, frag_names):
        if not self.enabled:
            return False
        import wave
        frames, sr, ch, sw = [], None, None, None
        for i, name in enumerate(frag_names):
            p = self._path(name)
            if p is None:
                log(f"voice: missing fragment '{name}' — announcement skipped")
                return False
            with wave.open(p) as w:
                fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
                if sr is None:
                    sr, ch, sw = fmt
                if fmt != (sr, ch, sw):
                    log(f"voice: fragment '{name}' format mismatch — skipped")
                    return False
                frames.append(w.readframes(w.getnframes()))
            if i < len(frag_names) - 1:
                frames.append(b"\x00" * int(sr * ch * sw * self.GAP_S))
        out = os.path.join(LOG_DIR, "last_voice.wav")
        with wave.open(out, "w") as w:
            w.setframerate(sr)
            w.setnchannels(ch)
            w.setsampwidth(sw)
            w.writeframes(b"".join(frames))
        try:
            level = self.volume
            if self.dyn:
                s = CURRENT.get("speed") or 0.0
                frac = min(s / self.road_km_h, 1.0)
                level = int(round(self.vol_idle
                                  + (self.vol_road - self.vol_idle) * frac))
                level = max(0, min(15, level))
            subprocess.call(["termux-volume", "music", str(level)])
            subprocess.Popen(["termux-media-player", "play", out])
        except OSError as e:
            log(f"voice: playback failed: {e}")
            return False
        log(f"voice: announced {' '.join(frag_names)} (vol {level})")
        return True

    def event(self, ev):
        """Map a detector event to voice fragments. Returns True if voiced."""
        e = ev["event"]
        if e == "regen_start":
            return self.speak(["regen_start"])
        if e == "regen_end":
            mins = min(self.MAX_N, int(ev.get("dur_s", 0) // 60))
            frags = ["p_finished", f"n{mins}"]
            if ev.get("km", 0) >= 1:
                km = min(self.MAX_N, int(ev["km"]))
                frags += ["minutes", "and", f"n{km}", "km_end"]
            else:
                frags += ["min_end"]
            return self.speak(frags)
        if e == "regen_interrupted":
            return self.speak(["regen_interrupted"])
        if e == "conn_up":
            return self.speak(["conn_up"])
        if e == "armed":
            return self.speak(["armed"])
        if e == "dtc_new":
            # Spell the code: P0670 -> p, d0, d6, d7, d0. Max 3 codes per
            # announcement keeps the sentence bounded (stored memories are
            # small; more codes are still logged and shown on the dash).
            frags = ["dtc_intro"]
            for idx, code in enumerate(ev.get("codes", [])[:3]):
                if idx:
                    frags.append("and")
                for ch in code:
                    frags.append(ch.lower() if ch.isalpha() else f"d{ch}")
            return self.speak(frags)
        if e == "dtc_cleared":
            return self.speak(["dtc_cleared"])
        if e == "dtc_clear_failed":
            return self.speak(["dtc_clear_failed"])
        return False


class ELM:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = None

    def connect(self, deadline_s=60):
        end = time.time() + deadline_s
        while time.time() < end:
            try:
                self.s = socket.create_connection((self.cfg["host"], self.cfg["port"]), timeout=10)
                self.s.settimeout(50)
                return True
            except OSError:
                time.sleep(5)
        return False

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass
        self.s = None

    def cmd(self, hexstr, timeout=45.0):
        if self.s is None:
            return ""
        try:
            self.s.sendall((hexstr.strip() + "\r").encode())
        except OSError:
            self.close()
            return ""
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = self.s.recv(1024)
            except socket.timeout:
                break
            except OSError:
                # BLE link died mid-command (reset/pipe): dead socket, not a
                # slow one — close so the next cmd() fails fast into the
                # ECU-lost path instead of raising into the main loop.
                self.close()
                break
            if not chunk:
                self.close()
                break
            buf += chunk
            if buf.endswith(b">"):
                break
        time.sleep(0.05)
        return buf.decode(errors="replace").strip()

    def setup(self):
        """Explicit ATSP init (config elm_proto), mirroring the exact manual
        sequence that works on this clone (verified live 2026-11-04 18:24):
        warm reset, echo/linefeeds off, ATSP, then 0100 triggers the 5-baud
        init. NO ATIIA — this clone mangles the init with it in explicit-
        proto mode, and the ELM default init address IS 0x10 (VAG K-line).
        ATH0/ATS0 go AFTER a successful init so formatting can never poison
        it. ATSP 0 auto worked 2026-11-02 but broke after power cycles —
        clone firmware quirks are NOT stable across resets.
        Returns (ok, raw) — ok if the legislated session answered (41 00 ...)."""
        self.cmd("ATWS", 12)
        for c in ("ATE0", "ATL0"):
            self.cmd(c, 8)
        self.cmd(f"ATSP {self.cfg['elm_proto']}", 8)
        r = self.cmd("0100", 50)  # triggers the 5-baud init
        ok = "4100" in r.replace(" ", "")
        self.fuel_ok = False
        if ok:
            for c in ("ATH0", "ATS0"):
                self.cmd(c, 8)
            # Fuel-rate capability probe (2026-11-05): mode-01 PID 5E, engine
            # fuel rate, (A*256+B)*0.05 L/h. If the ECU does not support it
            # (NO DATA) the daemon never asks again this session.
            try:
                r5e = self.cmd("0105E", 15)
                self.fuel_ok = "415E" in r5e.replace(" ", "")
            except Exception:
                self.fuel_ok = False
        return ok, r

    def read_sample(self):
        out = {"coolant": None, "speed": None, "rpm": None}

        def val(resp, marker, nbytes, scale):
            compact = resp.replace(" ", "")
            i = compact.find(marker)
            if i < 0:
                return None
            j = i + len(marker)
            if len(compact) < j + 2 * nbytes:
                return None
            # byte-boundary check: reject if more hex follows immediately
            # (a longer payload means this is not a clean single-PID frame)
            nxt = compact[j + 2 * nbytes: j + 2 * nbytes + 1]
            if nxt and all(ch in "0123456789abcdefABCDEF" for ch in nxt):
                return None
            try:
                return scale(int(compact[j: j + 2 * nbytes], 16))
            except ValueError:
                return None

        r = self.cmd("0105", 15)
        out["coolant"] = val(r, "4105", 1, lambda x: x - 40)
        r = self.cmd("010D", 15)
        out["speed"] = val(r, "410D", 1, lambda x: x)
        r = self.cmd("010C", 15)
        out["rpm"] = val(r, "410C", 2, lambda x: x / 4)
        if getattr(self, "fuel_ok", False):
            r = self.cmd("0105E", 15)
            out["fuel_lph"] = val(r, "415E", 2, lambda x: x * 0.05)
        return out


def decode_dtcs(resp):
    """Parse a mode-03 response into stored codes, e.g. ['P0670'].

    Format on this clone (ATH0/ATS0, live-verified 2026-11-07): the ELM
    returns the 43 service echo followed by 2-byte DTC records, no count
    byte — '43067000000000' = P0670 plus two empty 0000 slots.
    OBD-II encoding: bits 15-14 letter (P/C/B/U), then four hex digits.
    Returns None when the response is unreadable (garbage, NO DATA, empty)
    — the caller must treat None as "don't know", never as "no codes":
    a garbage frame must not produce a false announcement or a false
    all-clear. [] means read OK and memory is clean.
    """
    if not resp or "NO DATA" in resp.upper():
        return None
    stripped = "".join(ch for ch in resp if ch not in " \r\n>")
    if not stripped or any(ch not in "0123456789abcdefABCDEF" for ch in stripped):
        return None
    i = stripped.find("43")
    if i < 0 or len(stripped) < i + 6:      # 43 + at least one full record
        return None
    data = stripped[i + 2:]
    rem = len(data) % 4
    if rem:
        log(f"dtc decode: dropped {rem} trailing hex chars (partial record)")
    data = data[: len(data) - rem]
    codes = []
    for k in range(0, len(data), 4):
        try:
            w = int(data[k:k + 4], 16)
        except ValueError:
            break
        if w:
            letter = "PCBU"[(w >> 14) & 3]
            codes.append(f"{letter}{(w >> 12) & 3:X}{(w >> 8) & 0xF:X}"
                         f"{(w >> 4) & 0xF:X}{w & 0xF:X}")
    return codes


def dtc_csv_write(code, event):
    """DTC log, separate file (the regen CSV schema stays frozen)."""
    path = os.path.join(LOG_DIR, f"springtide_dtc_{datetime.now().strftime('%Y%m%d')}.csv")
    new = not os.path.exists(path)
    try:
        with open(path, "a") as f:
            if new:
                f.write("ts,code,event\n")
            f.write(f"{datetime.now().strftime('%F %T')},{code},{event}\n")
    except OSError as e:
        log(f"WARNING: dtc csv write failed: {e}")


def poll_dtcs(elm, state):
    """One mode-03 poll: announce+log newly stored codes, forget vanished
    ones (a self-healed code may re-announce when it returns). Returns
    (current_codes, events). Unreadable polls keep the last known codes."""
    codes = decode_dtcs(elm.cmd("03", 15))
    if codes is None:
        log("dtc poll unreadable — keeping last known codes")
        return state.get("dtc") or [], []
    seen = state.get("dtc_seen", {})
    events = []
    new = [c for c in codes if c not in seen]
    gone = [c for c in seen if c not in codes]
    for c in new:
        seen[c] = time.time()
        dtc_csv_write(c, "seen")
        log(f"DTC stored: {c}")
    for c in gone:
        seen.pop(c, None)
        dtc_csv_write(c, "gone")
        log(f"DTC no longer stored: {c}")
    if new:
        events.append({"event": "dtc_new", "codes": new})
    if new or gone:
        state["dtc_seen"] = seen
        save_json(STATE_PATH, state)  # survive restarts (heartbeat is 30 min)
    state["dtc"] = codes
    return codes, events


def process_clear_request(elm, state):
    """Mode-04 fault-memory clear — driver-commanded ONLY. dash.py ('c') or
    --clear-dtc writes the request file; it survives ignition-off, so a
    request made at the end of a drive is honored at the next ECU session.
    Verifies with a re-read (03) before announcing success; on failure the
    request is parked as .failed (no silent retry loop)."""
    prev = sorted((state.get("dtc_seen") or {}).keys())
    log(f"clear request: sending mode 04 (stored: {', '.join(prev) if prev else 'none'})")
    elm.cmd("04", 20)
    after = decode_dtcs(elm.cmd("03", 15))
    if after is None or after:
        for c in prev:
            dtc_csv_write(c, "clear_failed")
        log(f"WARNING: fault clear failed (still stored: {after})")
        try:
            os.replace(CLEAR_DTC_CMD, CLEAR_DTC_CMD + ".failed")
        except OSError as e:
            log(f"WARNING: could not park clear request ({e}) — removing it")
            try:
                os.unlink(CLEAR_DTC_CMD)
            except OSError as e2:
                log(f"WARNING: could not remove clear request: {e2}")
        # driver feedback, not just the log: the button press failed, say so
        # (the project rule: nothing vanishes silently)
        return {"event": "dtc_clear_failed", "codes": prev}
    for c in prev:
        dtc_csv_write(c, "cleared")
    state["dtc_seen"] = {}
    state["dtc"] = []
    for stale in (CLEAR_DTC_CMD, CLEAR_DTC_CMD + ".failed"):
        try:
            os.unlink(stale)
        except OSError:
            pass
    log("fault memory cleared")
    return {"event": "dtc_cleared", "codes": prev}


class Detector:
    """Coolant regen detector, absolute threshold form (2026-11-04):
    92-93 C = regeneration, up to 91 C is normal. Baseline is used only
    for arming and cold-engine reset, not for the regen math.

    Baseline hygiene (dual-model review 2026-11-02, kept):
    - the rolling window only feeds on idle/normal samples — never on
      candidate/regen temperatures (they would drag the median up)
    - during candidate/regen the baseline is the FROZEN value stored at
      candidate entry; a restart mid-regen resumes with the frozen value
    - cold engine clears samples + episode: fresh baseline every drive
    """

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.samples = []  # (ts, coolant) warm, non-elevated samples

    # -- helpers ---------------------------------------------------------
    def phase(self):
        return self.state.get("phase", "idle")

    def _set(self, **kw):
        self.state.update(kw)
        self.state["updated"] = time.time()

    def in_regen(self):
        return self.phase() == "regen"

    def n_warm(self):
        return sum(1 for (_t, c) in self.samples if c >= self.cfg["warm_c"] - 5)

    def _clear_episode(self):
        for k in ("cand_since", "regen_start", "regen_peak", "regen_km",
                  "clear_since", "frozen_baseline", "cand_dip_since"):
            self.state.pop(k, None)

    # -- core ------------------------------------------------------------
    def update(self, ts, coolant, speed_kmh):
        """Returns a list of events: dicts {event, ...}."""
        ev = []
        phase = self.phase()

        # Cold engine: fresh baseline per drive.
        if coolant < self.cfg["warm_c"]:
            if phase == "regen":
                ev.append(self._end_event(ts, note="engine cold"))
            if phase != "idle":
                self._set(phase="idle")
            self._clear_episode()
            self.samples = []
            self.state["baseline"] = None
            return ev

        # The baseline window feeds ONLY on non-elevated samples.
        if phase in ("idle", "normal"):
            self.samples.append((ts, coolant))
            cut = ts - self.cfg["baseline_s"]
            self.samples = [(t, c) for (t, c) in self.samples if t >= cut]

        warm = [c for (t, c) in self.samples if c >= self.cfg["warm_c"] - 5]
        if phase in ("candidate", "regen"):
            baseline = self.state.get("frozen_baseline")
        else:
            baseline = statistics.median(warm) if len(warm) >= 5 else None
        self.state["baseline"] = baseline

        if phase == "idle":
            if baseline is not None and len(warm) >= self.cfg["baseline_min_samples"]:
                self._set(phase="normal")
                ev.append({"event": "armed", "baseline": baseline})
            return ev

        if baseline is None:
            # No usable baseline mid-episode (restart with older state, or
            # a window that went empty). Never vanish silently: report the
            # loss if a regen was active (review 2026-11-02).
            if phase == "regen":
                ev.append({"event": "regen_interrupted", "note": "monitor restart"})
            self._set(phase="idle")
            self._clear_episode()
            return ev

        if phase == "normal":
            if coolant >= self.cfg["regen_thresh_c"]:
                self.state.pop("cand_dip_since", None)
                self._set(phase="candidate", cand_since=ts,
                          frozen_baseline=baseline)
            return ev

        if phase == "candidate":
            # Grace-dip confirm (2026-11-05, design ruling 1+3): a single
            # sub-threshold sample must NOT reset the confirm window — the
            # ride-2 regen signal sat at 92 with 1 C jitter and never
            # confirmed. Dips shorter than cand_grace_s pause the candidate;
            # only a sustained dip disqualifies it. Confirm fires on
            # candidate AGE, tolerating those brief dips.
            if coolant < self.cfg["regen_thresh_c"]:
                dip = self.state.get("cand_dip_since")
                if dip is None:
                    self.state["cand_dip_since"] = ts
                elif ts - dip >= self.cfg.get("cand_grace_s",
                                               self.cfg["clear_s"]):
                    self._set(phase="normal")
                    for k in ("cand_since", "frozen_baseline",
                              "cand_dip_since"):
                        self.state.pop(k, None)
            else:
                self.state.pop("cand_dip_since", None)
                if ts - self.state.get("cand_since", ts) >= self.cfg["confirm_s"]:
                    self._set(phase="regen", regen_start=ts,
                              regen_peak=coolant, regen_km=0.0)
                    self.state.pop("cand_since", None)
                    ev.append({"event": "regen_start", "coolant": coolant,
                               "baseline": baseline})
            return ev

        if phase == "regen":
            self.state["regen_peak"] = max(self.state.get("regen_peak", coolant), coolant)
            self.state["regen_km"] = self.state.get("regen_km", 0.0) + \
                (speed_kmh or 0.0) * self.cfg["poll_s"] / 3600.0
            if ts - self.state.get("regen_start", ts) > self.cfg["max_regen_s"]:
                ev.append(self._end_event(ts, note="timeout after 90 min"))
                self._set(phase="normal")
                self._clear_episode()
            elif coolant < self.cfg["regen_thresh_c"]:
                since = self.state.get("clear_since")
                if since is None:
                    self.state["clear_since"] = ts
                elif ts - since >= self.cfg["clear_s"]:
                    ev.append(self._end_event(ts))
                    self._set(phase="normal")
                    self._clear_episode()
            else:
                self.state.pop("clear_since", None)
            return ev

        return ev

    def _end_event(self, ts, note=None):
        start = self.state.get("regen_start")
        dur = (ts - start) if start else 0
        if not start and note is None:
            note = "unknown start"
        return {"event": "regen_end",
                "dur_s": dur,
                "km": self.state.get("regen_km", 0.0),
                "peak": self.state.get("regen_peak"),
                "note": note}

    def ecu_lost(self):
        """Called when the ECU disappears (ignition off)."""
        ev = []
        if self.in_regen():
            ev.append({"event": "regen_interrupted"})
        self._set(phase="idle")
        self._clear_episode()
        self.samples = []
        return ev


def fmt_event(ev):
    if ev["event"] == "armed":
        return None  # informational only
    if ev["event"] == "regen_start":
        return f"Springtide: regeneration started (coolant {ev['coolant']:.0f} C)"
    if ev["event"] == "regen_end":
        d = int(ev["dur_s"] // 60)
        msg = (f"Springtide: regeneration finished — {d} min"
               f"{f', ~{ev['km']:.0f} km' if ev.get('km', 0) >= 1 else ''}"
               f"{f', peak {ev['peak']:.0f} C' if ev.get('peak') else ''}")
        if ev.get("note"):
            msg += f" ({ev['note']})"
        return msg
    if ev["event"] == "regen_interrupted":
        msg = ("Springtide: engine off during regeneration — interrupted. "
               "It should resume automatically on the next drive.")
        if ev.get("note"):
            msg += f" ({ev['note']})"
        return msg
    if ev["event"] == "dtc_new":
        return ("Springtide: engine fault stored — " + ", ".join(ev.get("codes", []))
                + ". Press 'c' on the dash to clear the fault memory.")
    if ev["event"] == "dtc_cleared":
        codes = ev.get("codes") or []
        return ("Springtide: fault memory cleared"
                + (f" (was: {', '.join(codes)})" if codes else "."))
    if ev["event"] == "dtc_clear_failed":
        codes = ev.get("codes") or []
        return ("Springtide: fault clear FAILED — still stored: "
                + (", ".join(codes) if codes else "unknown")
                + ". Press 'c' again to retry.")
    return None


def dump_live(det, state, sample, ecu_up):
    """One compact dict per loop for dash.py (in-car status screen)."""
    start = state.get("regen_start")
    in_regen = det.phase() == "regen"
    save_json(LIVE_PATH, {
        "ts": time.time(),
        "phase": det.phase(),
        "coolant": None if sample is None else sample.get("coolant"),
        "baseline": state.get("baseline"),
        "n_warm": det.n_warm(),
        "speed": None if sample is None else sample.get("speed"),
        "rpm": None if sample is None else sample.get("rpm"),
        "ecu_up": ecu_up,
        "regen_elapsed_s": (time.time() - start) if (start and in_regen) else None,
        "regen_km": state.get("regen_km") if in_regen else None,
        "regen_peak": state.get("regen_peak") if in_regen else None,
        "last_event": state.get("last_event"),
        "last_event_t": state.get("last_event_t"),
        "dtc": state.get("dtc") or [],
        "dtc_clear_pending": os.path.exists(CLEAR_DTC_CMD),
    })


def main():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CMD_PATH, exist_ok=True)

    if "--test-voice" in sys.argv:
        i = sys.argv.index("--test-voice")
        which = sys.argv[i + 1] if len(sys.argv) > i + 1 else "test"
        ann = Announcer(cfg)
        if "--theme" in sys.argv:
            j = sys.argv.index("--theme")
            if len(sys.argv) > j + 1:
                ann.theme = sys.argv[j + 1]
                ann.dir = os.path.join(BASE, "voice", ann.theme)
                ann.enabled = os.path.isfile(os.path.join(ann.dir, "regen_start.wav"))
                if not ann.enabled:
                    print(f"theme {ann.theme}: fragments missing in {ann.dir}")
                    sys.exit(1)
        ann.dyn = False  # auditions play at full volume
        frags = {
            "test": ["test"],
            "start": ["regen_start"],
            "interrupted": ["regen_interrupted"],
            "up": ["conn_up"],
            "armed": ["armed"],
            "end": ["p_finished", "n23", "minutes", "and", "n84", "km_end"],
            "end-nokm": ["p_finished", "n23", "min_end"],
            "dtc": ["dtc_intro", "p", "d0", "d6", "d7", "d0"],
            "dtc-cleared": ["dtc_cleared"],
            "dtc-clear-failed": ["dtc_clear_failed"],
        }.get(which, ["test"])
        ok = ann.speak(frags)
        print("VOICE OK" if ok else "VOICE FAILED (disabled or fragments missing)")
        sys.exit(0 if ok else 1)

    if "--test-alert" in sys.argv:
        a = Alerter()
        ok = a.send("Springtide: test alert from the monitor phone. If you read this, the alert path works.")
        print("TEST ALERT SENT" if ok else "TEST ALERT FAILED")
        sys.exit(0 if ok else 1)

    if "--clear-dtc" in sys.argv:
        # Send-and-exit: queue a fault-memory clear for the daemon. Safe
        # beside the running daemon — it consumes the file itself.
        os.makedirs(CMD_PATH, exist_ok=True)
        with open(CLEAR_DTC_CMD, "w") as f:
            f.write(str(time.time()))
        print("CLEAR REQUEST QUEUED — the daemon clears at the next ECU session")
        sys.exit(0)

    state = load_json(STATE_PATH, {})
    alerter = Alerter() if cfg.get("telegram", {}).get("enabled") else None
    announcer = Announcer(cfg)
    det = Detector(cfg, state)
    elm = ELM(cfg)

    log(f"springtide daemon start (pid {os.getpid()}), phase={det.phase()}, "
        f"voice={'on ' + announcer.lang if announcer.enabled else 'off'}, "
        f"telegram={'on' if alerter else 'off'}")
    miss = 0
    session_up = False
    last_beat = time.time()
    last_dtc_poll = 0.0  # 0 -> first poll fires right after session-up

    while True:
        # retry any queued remote alert first (telegram-only concept)
        if alerter is not None:
            pending = state.get("pending_alert")
            if pending and alerter.send(pending):
                log("queued alert delivered")
                state.pop("pending_alert", None)
                save_json(STATE_PATH, state)

        if not session_up:
            if not elm.connect(deadline_s=cfg["offline_probe_s"]):
                log("bridge port closed (dongle off/out of range)")
                dump_live(det, state, None, False)
                time.sleep(cfg["offline_probe_s"])
                continue
            ok, raw = elm.setup()
            if not ok:
                log(f"ECU not answering (ignition off?) — re-probing. raw={raw[:60]!r}")
                for ev in det.ecu_lost():
                    queue(state, alerter, announcer, ev)
                elm.close()
                dump_live(det, state, None, False)
                time.sleep(cfg["offline_probe_s"])
                continue
            session_up = True
            miss = 0
            log(f"ECU session up (legislated KWP via ATSP {cfg['elm_proto']})")
            # "Connection established, monitoring activated" (2026-11-05):
            # announced on every session-up (ignition on, and again after any
            # mid-drive ECU loss/reconnect). NOT via queue() — lifecycle event,
            # must not clobber state["last_event"].
            announcer.event({"event": "conn_up"})

        t0 = time.time()
        sample = elm.read_sample()
        ts = time.time()
        CURRENT["speed"] = sample.get("speed") or 0.0

        if sample["coolant"] is None:
            miss += 1
            log(f"bad sample ({miss}/{cfg['ecu_miss_limit']}): {sample}")
            if miss >= cfg["ecu_miss_limit"]:
                log("ECU lost")
                for ev in det.ecu_lost():
                    queue(state, alerter, announcer, ev)
                elm.close()
                session_up = False
                dump_live(det, state, None, False)
            time.sleep(cfg["poll_s"])
            continue
        miss = 0

        # CSV row
        csv_path = os.path.join(LOG_DIR, f"springtide_{datetime.now().strftime('%Y%m%d')}.csv")
        new_file = not os.path.exists(csv_path)
        with open(csv_path, "a") as f:
            if new_file:
                f.write("ts,coolant_c,speed_kmh,rpm,phase,baseline_c\n")
            f.write(f"{datetime.now().strftime('%F %T')},{sample['coolant']},"
                    f"{sample['speed']},{sample['rpm']},{det.phase()},"
                    f"{state.get('baseline')}\n")

        # Fuel rate, separate file (2026-11-05): keeps the regen CSV schema
        # stable regardless of PID 5E support; l/100km integrates both files.
        fuel = sample.get("fuel_lph")
        if fuel is not None:
            fcsv = os.path.join(LOG_DIR,
                                f"springtide_fuel_{datetime.now().strftime('%Y%m%d')}.csv")
            fnew = not os.path.exists(fcsv)
            with open(fcsv, "a") as f:
                if fnew:
                    f.write("ts,fuel_lph\n")
                f.write(f"{datetime.now().strftime('%F %T')},{fuel}\n")

        for ev in det.update(ts, sample["coolant"], sample.get("speed") or 0.0):
            if ev["event"] == "armed":
                # lifecycle event, journal-only by design: voice it
                # (2026-11-05 "let it play") but keep it OUT of queue() so
                # state["last_event"] keeps tracking regen events only.
                log(f"armed: baseline={ev['baseline']} — detection active")
                announcer.event(ev)
                continue
            queue(state, alerter, announcer, ev)

        # DTC layer (2026-11-07): stored fault memory over the same
        # legislated session. A queued clear request takes priority over
        # the periodic poll (both refresh the cadence timer).
        if session_up:
            if os.path.exists(CLEAR_DTC_CMD):
                ev = process_clear_request(elm, state)
                if ev:
                    queue(state, alerter, announcer, ev)
                last_dtc_poll = time.time()
            elif cfg["dtc_poll_s"] > 0 and time.time() - last_dtc_poll >= cfg["dtc_poll_s"]:
                last_dtc_poll = time.time()
                for ev in poll_dtcs(elm, state)[1]:
                    queue(state, alerter, announcer, ev)

        dump_live(det, state, sample, True)

        if det.phase() != "idle":
            save_json(STATE_PATH, state)  # keep episode context on restart
        if time.time() - last_beat > cfg["heartbeat_s"]:
            log(f"heartbeat: coolant={sample['coolant']}C phase={det.phase()} "
                f"baseline={state.get('baseline')}")
            last_beat = time.time()
            save_json(STATE_PATH, state)

        elapsed = time.time() - t0
        time.sleep(max(0.2, cfg["poll_s"] - elapsed))


def queue(state, alerter, announcer, ev):
    msg = fmt_event(ev)
    if not msg:
        return
    log(f"EVENT {ev['event']}: {msg}")
    state["last_event"] = ev["event"]
    state["last_event_t"] = time.time()
    if announcer.event(ev):
        # voiced in-car: the driver got the message, no remote transport needed
        save_json(STATE_PATH, state)
        return
    if alerter is not None:
        if alerter.send(msg):
            state.pop("pending_alert", None)
        else:
            state["pending_alert"] = msg  # retried next loop
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()