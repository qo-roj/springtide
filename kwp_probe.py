#!/data/data/com.termux/files/usr/bin/env python3
"""K-line probe for the VAG diesel — through the OBD bridge (127.0.0.1:35000).

DIAGNOSTIC TOOL — deliberately sends session/identification services
(10 86, 1A 9B, 21 xx reads, 3E 80) to DERIVE the protocol verdicts. This is
NOT the read-only monitoring component: springtide_daemon.py only ever sends mode-01
requests (0100/0105/010D/010C) and AT commands. NEVER send session 10 85
(programming session — probed once by accident 2026-11-02; harmless, no
writes, but it wedges the bus until an ignition cycle).

The engine ECU speaks ISO 14230-4 KWP, 5-baud init (verified by Car Scanner:
init addr 0x10, 10.4 kbaud, cal ID "459 601 D 477"). NOT CAN/UDS — mode-22
DIDs do not apply to this car.

Section A — legislated EOBD session (init addr 0x10, what Car Scanner uses):
    adapter identity, coolant PID 05, VIN (09 02), calibration ID (09 04).
Section B — VAG KWP2000 session (init addr 0x01, the VAG engine controller):
    ECU identification (1A 9B -> part number + soft version, ASCII),
    extended session (10 86), then measuring-block reads (21 <LID> 01).
    LID numbers mirror the KW1281/VCDS group numbers one-for-one
    (per VAG KWP2000 design). Default group list + optional --sweep 1-255.
    Values decoded with the VAG text-table (ported from jazdw/vag-blocks
    kwp2000.cpp, GPL-3 — decodeBlockData()).

Usage: python3 kwp_probe.py [--host 127.0.0.1] [--port 35000]
                             [--groups 1,2,70,72,240] [--sweep] [--csv out.csv]
"""
import argparse
import csv
import socket
import sys
import time

# VAG measuring-value text table: (a, b, id) -> (units, value)
def vag_decode(a, b, tid):
    if tid == 0x01: return ("rpm", a * b / 5.0)
    if tid == 0x04: return ("deg ATDC" if b > 127 else "deg BTDC", abs(b - 127) * 0.01 * a)
    if tid == 0x07: return ("km/h", 0.01 * a * b)
    if tid in (0x08, 0x10, 0x25): return ("Binary", (a << 8) | b)
    if tid == 0x11: return ("ASCII", chr(a) + chr(b))
    if tid == 0x12: return ("mbar", a * b / 25.0)
    if tid == 0x14: return ("%", a * b / 128.0 - 1)
    if tid == 0x15: return ("V", a * b / 1000.0)
    if tid == 0x16: return ("ms", 0.001 * a * b)
    if tid == 0x17: return ("%", b * a / 256.0)
    if tid == 0x1A: return ("degC", b - a)
    if tid == 0x21: return ("%", 100.0 * b if a == 0 else 100.0 * b / a)
    if tid == 0x22: return ("kW", (b - 128) * 0.01 * a)
    if tid == 0x23: return ("l/h", a * b / 100.0)
    if tid == 0x27: return ("mg/stk(fuel)", a * b / 256.0)
    if tid == 0x31: return ("mg/stk(air)", a * b / 40.0)
    if tid == 0x33: return ("mg/stk delta", ((b - 128) / 255.0) * a)
    if tid == 0x36: return ("Count", a * 256 + b)
    if tid == 0x37: return ("s", a * b / 200.0)
    if tid == 0x51: return ("torque", ((a * 112000.0) + (b * 436.0)) / 1000.0)
    if tid == 0x5E: return ("Nm", a * (b / 50.0 - 1))
    return (None, None)

DEFAULT_GROUPS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20,
                  30, 32, 34, 36, 40, 41, 42, 43, 50, 51, 54, 55, 60, 61,
                  70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80,
                  90, 91, 92, 93, 94, 95, 100, 101, 102, 103, 104, 105,
                  110, 111, 112, 113, 114, 115, 120, 125, 130, 140, 150,
                  240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255]


def toks(raw):
    out = []
    for t in raw.replace("\r", " ").replace(">", " ").replace(".", " ").split():
        if len(t) == 2 and all(c in "0123456789abcdefABCDEF" for c in t):
            out.append(t.upper())
    return out


def frame_payload(tokens):
    """Strip ISO 14230 framing (with header) if present, verify checksum best-effort."""
    if not tokens:
        return tokens, False
    t0 = int(tokens[0], 16)
    if t0 & 0x80 and (t0 & 0x0F) != 0:
        hdr = 3                       # single frame: fmt|len, tgt, src
    elif t0 & 0xC0 == 0x80:
        hdr = 5                        # 2-byte length: fmt(0x80), lenHi, lenLo, tgt, src
    else:
        hdr = 0                        # no framing visible — treat all as payload
    body = tokens[hdr:]
    if body and hdr > 0:
        # checksum = sum of header+payload mod 256; drop trailing byte when it matches
        try:
            s = sum(int(x, 16) for x in tokens[:-1]) & 0xFF
            if s == int(body[-1], 16):
                body = body[:-1]
        except ValueError:
            pass
    return body, True


class ELM:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=15)
        self.s.settimeout(8)

    def cmd(self, hexstr, timeout=8.0, label=""):
        self.s.sendall((hexstr.strip() + "\r").encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = self.s.recv(1024)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b">"):
                break
        time.sleep(0.03)
        return buf.decode(errors="replace").strip()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def elm_setup(e, init_addr):
    """Reset + configure ELM for ISO 14230-4 KWP init at the given address.
    NOTE: this clone's explicit ATSP 5 is BROKEN (BUS INIT: ERROR) — ATSP 0
    auto-negotiation works and correctly settles on ISO 14230-4 KWP 5BAUD,
    honouring ATIIA. Always use ATSP 0 here."""
    e.cmd("ATZ", timeout=6)
    e.cmd("ATE0")
    e.cmd("ATL0")
    e.cmd("ATH1")          # show framing — needed for parsing
    e.cmd("ATS1")
    e.cmd("ATSP 0")        # AUTO — the only init path that works on this clone
    r = e.cmd(f"ATIIA {init_addr:02X}", label="IIA")
    if "?" in r or "ERROR" in r.upper():
        print(f"!! ATIIA not supported by this clone: {r!r}")
        return False
    return True


def section_a(e):
    print("\n========== SECTION A: legislated EOBD (init addr 0x10) ==========")
    elm_setup(e, 0x10)
    results = []
    for c, lab, to in [("0100", "CAN-negotiate / PIDs supported", 16),
                       ("0105", "coolant temp (PID 05)", 8),
                       ("0902", "VIN", 10),
                       ("0904", "calibration ID", 10)]:
        r = e.cmd(c, timeout=to)
        results.append(r)
        print(f"{c} {lab:<30} -> {r!r}")
        toks_ = toks(r)
        if c == "0105":
            body, _ = frame_payload(toks_)
            if "41" in body:
                i = body.index("41")
                if i + 2 < len(body) and body[i + 1] == "05":
                    print(f"     coolant = {int(body[i + 2], 16) - 40} degC")
        if c == "0902":
            body, _ = frame_payload(toks_)
            if "49" in body and body.index("49") + 3 < len(body):
                i = body.index("49")
                if body[i + 1] == "02":
                    hx = body[i + 3:]   # skip 49 02 <item-count>
                    try:
                        vin = bytes(int(x, 16) for x in hx).decode("ascii", errors="replace").strip("\x00 ")
                        print(f"     VIN     = {vin}")
                    except Exception:
                        pass

    if results and all("BUS INIT" in r for r in results):
        print("\n!!! BUS INIT FAILED on the legislated session too — the ECU is not answering the")
        print("    K-line at all. Check IGNITION ON (dashboard lit, engine off is fine) and")
        print("    adapter seating. Everything below is meaningless until the bus init works.")
        return False
    return True


def section_b(e, groups, csv_path, bus_ok=True):
    print("\n========== SECTION B: VAG KWP2000 (init addr 0x01) ==========")
    if not elm_setup(e, 0x01):
        print("ATIIA unsupported -> VAG session impossible with this clone.")
        print("Verdict: coolant-fallback mode, or Plan B (KKL OTG cable, KW1281).")
        return None
    print("initialising 5-baud K-line at VAG engine address (takes ~5-7 s)...")
    ident = e.cmd("1A9B", timeout=18)
    print(f"1A9B ECU identification -> {ident!r}")
    body, _ = frame_payload(toks(ident))
    if body and body[0] == "5A":
        try:
            txt = bytes(int(x, 16) for x in body[3:] if x != "?").decode("ascii", errors="replace")
            print(f"     ECU ID = {txt!r}")
        except Exception:
            pass
    elif "7F" in body:
        print("     negative response to 1A9B -> ECU does not answer KWP2000-VAG.")
        print("     Likely KW1281-only. ELM327 cannot speak KW1281.")
        print("     Verdict: coolant-fallback, or Plan B (KKL OTG cable).")
        return None

    e.cmd("1086", timeout=8)          # extended diagnostic session (VAG 0x86)
    print("10 86 extended session set")

    rows = []
    answered = 0
    for g in groups:
        r = e.cmd(f"21{g:02X}01", timeout=4)
        body, _ = frame_payload(toks(r))
        if not body:
            status = "NO RESPONSE"
        elif body[0] == "7F":
            nrc = body[2] if len(body) > 2 else "?"
            status = f"NEG (NRC {nrc})"
        elif body[0] == "61":
            vals = body[3:] if len(body) > 3 and body[1] == f"{g:02X}"[:2] else body[1:]
            decoded = []
            for i in range(0, len(vals) - 2, 3):
                a, b, tid = int(vals[i], 16), int(vals[i + 1], 16), int(vals[i + 2], 16)
                u, v = vag_decode(a, b, tid)
                if u is None:
                    u, v = vag_decode(b, tid, a)  # alt byte order
                decoded.append(f"{v:.2f} {u}" if u else f"[{vals[i]} {vals[i+1]} {vals[i+2]}]")
            status = "OK: " + (" | ".join(decoded) if decoded else "(empty)")
            answered += 1
        else:
            status = "ODD: " + " ".join(body)
        print(f"21 {g:02X} 01  -> {status}")
        rows.append([g, r.replace("\r", " ").replace(">", ""), status])
        if g % 4 == 0:
            e.cmd("3E80", timeout=3)  # tester present, suppress response
        time.sleep(0.1)

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["group", "raw", "decoded"])
            w.writerows(rows)
        print(f"\nCSV written: {csv_path}")

    print("\n========== VERDICT ==========")
    print(f"groups answered: {answered}/{len(groups)}")
    if answered >= 10:
        print("KWP2000-VAG WORKS. VCDS label file (once engine known) maps group numbers")
        print("to labels; the DPF groups (EGT/soot/regen) are in this sweep's output.")
        print("=> PRIMARY MODE: real DPF monitoring on this ECU.")
    elif answered > 0:
        print("Partial answers — inspect the CSV; some ECUs only expose a few groups.")
    else:
        if not bus_ok:
            print("No measuring blocks AND the legislated bus init failed earlier —")
            print("this is NOT a protocol verdict. Fix the bus init (ignition ON) and re-run.")
        else:
            print("No measuring blocks via KWP2000 -> KW1281-only likely. Options:")
            print("  1) coolant-fallback monitor (mode 01 PID 05) — works today")
            print("  2) Plan B: ~10 EUR dumb KKL USB cable + phone OTG + KW1281 (full blocks)")
    return answered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=35000)
    ap.add_argument("--groups", default="", help="comma-separated, overrides defaults")
    ap.add_argument("--sweep", action="store_true", help="sweep ALL groups 1..255")
    ap.add_argument("--csv", default="kwp_sweep.csv")
    args = ap.parse_args()

    if args.sweep:
        groups = list(range(1, 256))
    elif args.groups:
        groups = [int(x) for x in args.groups.split(",") if x.strip()]
    else:
        groups = DEFAULT_GROUPS

    print(f"connecting {args.host}:{args.port} (bridge) ...")
    try:
        e = ELM(args.host, args.port)
    except OSError as ex:
        print(f"cannot connect: {ex} (bridge up? dongle connected?)")
        sys.exit(1)

    print("ATI adapter ->", e.cmd("ATI", timeout=6))
    print("ATRV voltage ->", e.cmd("ATRV", timeout=6))
    a_ok = section_a(e)
    section_b(e, groups, args.csv, a_ok)
    e.close()


if __name__ == "__main__":
    main()