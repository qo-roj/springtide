#!/data/data/com.termux/files/usr/bin/env python3
"""ELM327 probe through the OBD bridge (127.0.0.1:35000).

AT init + adapter identity + generic coolant PID + the VAG mode-22 DPF DIDs,
with an extended-session (1003) retry for DIDs that refuse in the default session.
Prints a verdict per DID so the primary-vs-fallback decision is grounded in live data.

Usage: python3 elm_probe.py [--host 127.0.0.1] [--port 35000]
"""
import argparse
import socket
import sys
import time

DIDS = [
    # (request, label, decode)
    ("22114E", "soot mass measured [g]",   lambda b: ((b[0] << 8) | b[1]) / 100),
    ("22114F", "soot mass calculated [g]", lambda b: ((b[0] << 8) | b[1]) / 100),
    ("221156", "km since last regen",      lambda b: ((b[2] << 8) | b[3]) / 1000),
    ("22115E", "min since last regen",     lambda b: ((b[2] << 8) | b[3]) / 60),
    ("2211B2", "DPF input temp [C]",       lambda b: (((b[0] << 8) | b[1]) - 2731) / 10),
    ("2210F9", "DPF output temp [C]",      lambda b: (((b[0] << 8) | b[1]) - 2731) / 10),
    ("22178C", "oil ash residue [g]",      lambda b: ((b[0] << 8) | b[1]) / 10),
]


def hex_tokens(raw):
    toks = []
    for t in raw.replace("\r", " ").replace(">", " ").split():
        if all(ch in "0123456789abcdefABCDEF" for ch in t):
            toks.append(t.upper())
    return toks


def decode_mode22(raw, did, fn):
    """Response shape: ... 62 11 4E <data...>. Returns value or None."""
    toks = hex_tokens(raw)
    want = did[2:]  # '22114E' -> '114E'
    for i, t in enumerate(toks):
        if t == "62" and i + 2 < len(toks):
            d = (toks[i + 1] + toks[i + 2]).upper()
            if d == want:
                data = toks[i + 3:]
                if len(data) >= 4:
                    b = [int(x, 16) for x in data[:4]]
                    return fn(b)
                return None
    return None


def ascii_decode(raw, did):
    """Extract ASCII payload from a 0x62 response (echoed DID then text)."""
    toks = hex_tokens(raw)
    want = did[2:]
    for i, t in enumerate(toks):
        if t == "62" and i + 2 < len(toks):
            d = (toks[i + 1] + toks[i + 2]).upper()
            if d == want:
                data = toks[i + 3:]
                try:
                    return bytes(int(x, 16) for x in data).decode("ascii", errors="replace").strip("\x00 ")
                except Exception:
                    return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=35000)
    args = ap.parse_args()

    print(f"connecting {args.host}:{args.port} ...")
    try:
        s = socket.create_connection((args.host, args.port), timeout=15)
    except OSError as e:
        print(f"cannot connect (bridge not ready / dongle not connected?): {e}")
        sys.exit(1)
    s.settimeout(6)

    def cmd(c, timeout=6.0):
        s.sendall((c + "\r").encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = s.recv(512)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b">"):
                break
        time.sleep(0.05)
        return buf.decode(errors="replace").strip()

    print("\n-- ELM init --")
    for c in ("ATZ", "ATE0", "ATL0", "ATS0"):
        print(f"{c:<8} -> {cmd(c)!r}")
        time.sleep(0.15)
    ident = cmd("ATI")
    print(f"{'ATI':<8} -> {ident!r}   <- adapter firmware identity")
    print(f"{'ATRV':<8} -> {cmd('ATRV')!r}  <- battery voltage")

    print("\n-- generic OBD (default header) --")
    print(f"{'0100':<8} -> {cmd('0100')!r}  <- CAN negotiation")
    cool = cmd("0105")
    print(f"{'0105':<8} -> {cool!r}  <- coolant temp")
    toks = hex_tokens(cool)
    if "41" in toks and "05" in toks:
        i = toks.index("05", toks.index("41"))
        if i + 1 < len(toks):
            print(f"           coolant = {int(toks[i + 1], 16) - 40} C")

    print("\n-- VAG header 7E0 --")
    print(f"{'ATSH7E0':<8} -> {cmd('ATSH 7E0')!r}")

    print("\n-- ECU identification (UDS 0x22) --")
    for did, label in [("22F187", "ECU part number"), ("22F190", "VIN"), ("22F197", "system name")]:
        raw = cmd(did)
        txt = ascii_decode(raw, did)
        print(f"{did} {label:<16} -> {raw!r}" + (f"  => {txt}" if txt else ""))

    print("\n-- mode-22 DPF DIDs (default session) --")
    results, failed = {}, []
    for did, label, fn in DIDS:
        raw = cmd(did)
        val = decode_mode22(raw, did, fn)
        results[did] = val
        mark = "OK" if val is not None else "--"
        print(f"{did} {label:<26} {mark} {raw!r}")
        if val is None:
            failed.append((did, label, fn))

    if failed:
        print("\n-- extended session (1003) retry for the failures --")
        print(f"{'1003':<8} -> {cmd('1003')!r}")
        for did, label, fn in failed:
            raw = cmd(did)
            val = decode_mode22(raw, did, fn)
            results[did] = val
            print(f"{did} {label:<26} {'OK' if val is not None else '--'} {raw!r}")

    print("\n-- summary --")
    ok = [d for d, v in results.items() if v is not None]
    if ok:
        print(f"ANSWERING DIDs ({len(ok)}/{len(DIDS)}):")
        for did, label, fn in DIDS:
            if results[did] is not None:
                print(f"  {did} {label:<26} = {results[did]}")
        print("=> PRIMARY MODE: soot/regen DIDs work on this engine. Build the full monitor.")
    else:
        print("NO DIDs answered -> run coolant-fallback mode (PID 05 heuristic),")
        print("   and try the free 'VAG DPF lite' app to cross-check engine support.")
    s.close()


if __name__ == "__main__":
    main()