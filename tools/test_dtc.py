#!/usr/bin/env python3
"""Hardware-free tests for the DTC layer (decode/poll/clear/announce).

Run on the repo root: python3 tools/test_dtc.py
Covers the live-verified clone format ('43067000000000' = P0670, ATH0/ATS0),
the None-vs-[] contract, and the dash/CLI clear-request flow.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import springtide_daemon as sd  # noqa: E402

os.makedirs(sd.LOG_DIR, exist_ok=True)
os.makedirs(sd.CMD_PATH, exist_ok=True)

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


class FakeELM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def cmd(self, hexstr, timeout=15.0):
        self.sent.append(hexstr)
        return self.responses.pop(0) if self.responses else ""


# -- decode_dtcs -----------------------------------------------------------
print("decode_dtcs:")
check("live clone format P0670",
      sd.decode_dtcs("43067000000000\r\r>") == ["P0670"])
check("cleared memory -> []",
      sd.decode_dtcs("43000000000000\r\r>") == [])
check("spaced ELM output",
      sd.decode_dtcs("43 06 70 00 00 00 00\r\r>") == ["P0670"])
check("multiple codes",
      sd.decode_dtcs("43067001350420 00 00") == ["P0670", "P0135", "P0420"],
      str(sd.decode_dtcs("43067001350420 00 00")))
check("NO DATA -> None", sd.decode_dtcs("NO DATA\r\r>") is None)
check("empty -> None", sd.decode_dtcs("") is None)
check("garbage text -> None", sd.decode_dtcs("SEARCHING...\r\r>") is None)
check("CAN ERROR -> None", sd.decode_dtcs("CAN ERROR\r\r>") is None)
check("bare prompt -> None", sd.decode_dtcs("\r\r>") is None)
check("43 without a full record -> None", sd.decode_dtcs("4306") is None)
check("odd-length tail tolerated",
      sd.decode_dtcs("43067000000000F") == ["P0670"])
# letter bits: P=00 C=01 B=10 U=11 in bits 15-14 of the 16-bit word
check("C1234 -> 0x5234", sd.decode_dtcs("4352340000000000 00") == ["C1234"],
      str(sd.decode_dtcs("4352340000000000 00")))
check("U1000 -> 0xD000", sd.decode_dtcs("43 D0 00 00 00 00 00") == ["U1000"],
      str(sd.decode_dtcs("43 D0 00 00 00 00 00")))
check("B1234 -> 0x9234", sd.decode_dtcs("43923400000000000") == ["B1234"],
      str(sd.decode_dtcs("43923400000000000")))
check("P0670 roundtrip", sd.decode_dtcs("43 06 70 00 00 00 00") == ["P0670"])

# -- poll_dtcs --------------------------------------------------------------
print("poll_dtcs:")
elm = FakeELM(["43067000000000\r\r>"])
state = {"dtc_seen": {}, "dtc": []}
codes, evs = sd.poll_dtcs(elm, state)
check("codes returned", codes == ["P0670"], str(codes))
check("dtc_new event", evs == [{"event": "dtc_new", "codes": ["P0670"]}], str(evs))
check("state.dtc updated", state["dtc"] == ["P0670"])
check("state.dtc_seen updated", "P0670" in state["dtc_seen"])

# second poll, same code: no re-announce
elm = FakeELM(["43067000000000\r\r>"])
codes, evs = sd.poll_dtcs(elm, state)
check("same code silent", evs == [], str(evs))

# unreadable poll: keeps last known, no events
elm = FakeELM(["CAN ERROR\r\r>"])
codes, evs = sd.poll_dtcs(elm, state)
check("unreadable keeps codes", codes == ["P0670"], str(codes))
check("unreadable silent", evs == [], str(evs))
check("unreadable keeps dtc_seen", "P0670" in state["dtc_seen"])

# code vanished (self-healed): 'gone', forget it
elm = FakeELM(["43000000000000\r\r>"])
codes, evs = sd.poll_dtcs(elm, state)
check("gone -> clean list", codes == [], str(codes))
check("gone -> no events", evs == [], str(evs))
check("gone -> forgotten", state["dtc_seen"] == {}, str(state["dtc_seen"]))

# code returns after being forgotten: re-announces
elm = FakeELM(["43067000000000\r\r>"])
codes, evs = sd.poll_dtcs(elm, state)
check("returned code re-announces",
      evs == [{"event": "dtc_new", "codes": ["P0670"]}], str(evs))

# CSV was written
csv = os.path.join(sd.LOG_DIR, "springtide_dtc_"
                   + sd.datetime.now().strftime("%Y%m%d") + ".csv")
body = open(csv).read()
check("csv has seen/gone rows",
      "P0670,seen" in body and "P0670,gone" in body, body[:200])

# -- process_clear_request --------------------------------------------------
print("process_clear_request:")
state = {"dtc_seen": {"P0670": 1.0}, "dtc": ["P0670"]}
with open(sd.CLEAR_DTC_CMD, "w") as f:
    f.write("x")
elm = FakeELM(["44\r\r>", "43000000000000\r\r>"])  # 04 ok, 03 clean
ev = sd.process_clear_request(elm, state)
check("clear success event", ev == {"event": "dtc_cleared", "codes": ["P0670"]}, str(ev))
check("clear request file removed", not os.path.exists(sd.CLEAR_DTC_CMD))
check("cleared clears state", state["dtc_seen"] == {} and state["dtc"] == [])
check("04 then 03 sent", elm.sent == ["04", "03"], str(elm.sent))
body = open(csv).read()
check("csv cleared row", "P0670,cleared" in body)

# failure path: code survives the clear
state = {"dtc_seen": {"P0670": 1.0}, "dtc": ["P0670"]}
with open(sd.CLEAR_DTC_CMD, "w") as f:
    f.write("x")
elm = FakeELM(["44\r\r>", "43067000000000\r\r>"])
ev = sd.process_clear_request(elm, state)
check("clear failure -> dtc_clear_failed event",
      ev == {"event": "dtc_clear_failed", "codes": ["P0670"]}, str(ev))
check("request parked as .failed", os.path.exists(sd.CLEAR_DTC_CMD + ".failed"))
check("state kept on failure", "P0670" in state["dtc_seen"])
m = sd.fmt_event(ev or {})
check("fmt dtc_clear_failed", "FAILED" in m and "P0670" in m, str(m))
os.unlink(sd.CLEAR_DTC_CMD + ".failed")

# failure path 2: 03 unreadable after 04
state = {"dtc_seen": {"P0670": 1.0}, "dtc": ["P0670"]}
with open(sd.CLEAR_DTC_CMD, "w") as f:
    f.write("x")
elm = FakeELM(["44\r\r>", "NO DATA\r\r>"])
ev = sd.process_clear_request(elm, state)
check("unreadable verify -> failure event",
      ev == {"event": "dtc_clear_failed", "codes": ["P0670"]}, str(ev))
os.unlink(sd.CLEAR_DTC_CMD + ".failed")

# empty memory clear: still announces (driver feedback)
state = {"dtc_seen": {}, "dtc": []}
with open(sd.CLEAR_DTC_CMD, "w") as f:
    f.write("x")
open(sd.CLEAR_DTC_CMD + ".failed", "w").close()  # stale marker from an old failure
elm = FakeELM(["44\r\r>", "43000000000000\r\r>"])
ev = sd.process_clear_request(elm, state)
check("empty clear still gives feedback",
      ev == {"event": "dtc_cleared", "codes": []}, str(ev))
check("stale .failed swept on success", not os.path.exists(sd.CLEAR_DTC_CMD + ".failed"))

# -- announcer fragment mapping --------------------------------------------
print("announcer:")
ann = object.__new__(sd.Announcer)
ann.enabled = True
ann.dir = "/nonexistent"  # forces _path misses; we stub speak
captured = []
ann.speak = lambda frags: captured.append(frags) or True  # type: ignore[method-assign]
sd.Announcer.event(ann, {"event": "dtc_new", "codes": ["P0670", "U0100"]})
check("spelled fragments",
      captured[0] == ["dtc_intro", "p", "d0", "d6", "d7", "d0",
                       "and", "u", "d0", "d1", "d0", "d0"], str(captured[0]))
sd.Announcer.event(ann, {"event": "dtc_new", "codes": [f"P{i:03d}" for i in range(5)]})
check("max 3 codes per announcement", len([f for f in captured[1] if f == "dtc_intro"]) == 1
      and "and" in captured[1])
check("cap keeps first 3", captured[1][1] == "p" and "d0" in captured[1])
sd.Announcer.event(ann, {"event": "dtc_cleared", "codes": ["P0670"]})
check("cleared fragment", captured[-1] == ["dtc_cleared"])
sd.Announcer.event(ann, {"event": "dtc_clear_failed", "codes": ["P0670"]})
check("clear_failed fragment", captured[-1] == ["dtc_clear_failed"])

# -- fmt_event ---------------------------------------------------------------
m = sd.fmt_event({"event": "dtc_new", "codes": ["P0670"]}) or ""
check("fmt dtc_new mentions code and dash key",
      "P0670" in m and "'c'" in m, str(m))
m = sd.fmt_event({"event": "dtc_cleared", "codes": ["P0670"]}) or ""
check("fmt dtc_cleared", "cleared" in m and "P0670" in m, str(m))

# -- config default ----------------------------------------------------------
check("dtc_poll_s default", sd.DEFAULTS["dtc_poll_s"] == 60.0)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}: {FAILS}")
    sys.exit(1)
print(f"ALL PASS ({len(FAILS) == 0})")
