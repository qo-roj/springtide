#!/usr/bin/env python3
"""KW1281 on-car probe for Springtide: connect to the engine ECU (0x01)
over the KKL cable and read the DPF measuring blocks.

Run on the phone (root):
  su -c '/data/data/com.termux/files/usr/bin/python3 \
         /data/data/com.termux/files/home/dpf/kwp1281_probe.py [--groups 67,68]'

Groups (Seat Ibiza 6L 1.4 TDI BMS, VCDS label 045-906-013-BMS):
  001  engine basics (rpm / coolant cross-check at field 4)
  067  DPF exhaust gas I: pre-turbo EGT (regen verify >=690 C), DPF temp,
       diff pressure mbar (engine-off 0+-5), diff offset
  068  DPF load: load coefficient % (>50 regen, >75 limp, >90 prohibited),
       carbon mass g (max 50), learned value
  069  regen status: field 1 bit0 = regen INTERRUPTED; field 4 bitfield
  070  regen counters: field 1 bit0 = normal thermal regen ACTIVE,
       bit1 = enhanced; field 2 regen time total, 3 failed, 4 successful
  073  since last regen: consumption l/100km, km, minutes
  029  oil/wear/soot: field 4 soot index
  075  DPF temps II + load coefficient
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402
import kwp1281 as K  # noqa: E402

GROUP_NOTES = {
    1: "engine basics (field 4 = coolant, cross-check the 92 C rule)",
    29: "oil temp/level/wear + field 4 soot index",
    67: "DPF exhaust I: pre-turbo EGT (regen verify >=690), DPF temp, "
        "diff pressure mbar, diff offset",
    68: "DPF load: coefficient % (>50 regen, >75 limp, >90 prohibited), "
        "carbon mass g (max 50), learned",
    69: "regen status: field1 bit0=interrupted, field4 bitfield",
    70: "regen: f1 bit0=ACTIVE bit1=enhanced, f2 time total, 3 failed, "
        "4 successful",
    73: "since last regen: l/100km, km, minutes",
    75: "DPF temps II + load coefficient",
}
DEFAULT_GROUPS = [1, 67, 68, 69, 70, 73, 29, 75]


def render_bitfield(val):
    bits = [f"bit{i}" for i in range(8) if val & (1 << i)]
    return f"{val} ({'|'.join(bits) if bits else 'none'})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", default=",".join(map(str, DEFAULT_GROUPS)),
                    help="comma-separated group numbers")
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x01)
    ap.add_argument("--hex", action="store_true", help="dump raw frames")
    args = ap.parse_args()
    groups = [int(g) for g in args.groups.split(",") if g.strip()]

    log = (lambda direction, frame: print(
        f"  {direction}: {frame.hex()}")) if args.hex else None

    dev = F.UsbDevice(F.find_ft232r())
    dev.claim_interface(0)
    ftdi = F.Ftdi232R(dev)
    port = F.KKLPort(ftdi)
    port.open_10400()
    kwp = K.Kwp1281(port, log=log)

    print(f"connecting to ECU 0x{args.addr:02x} ...")
    try:
        idents = kwp.connect(args.addr)
    except (K.Kwp1281Error, F.KLineTimeout) as e:
        print(f"CONNECT FAILED: {type(e).__name__}: {e}")
        return 2
    print("ECU ident:")
    for s in idents:
        print(f"  {s!r}")

    try:
        for g in groups:
            try:
                fields = kwp.read_group(g)
            except (K.Kwp1281Error, F.KLineTimeout) as e:
                print(f"\n== group {g:03d} ({GROUP_NOTES.get(g, '')}) ==")
                print(f"  ERROR: {type(e).__name__}: {e}")
                continue
            print(f"\n== group {g:03d} ({GROUP_NOTES.get(g, '')}) ==")
            if fields is None:
                print("  (valid group, no measurements — ACK response)")
                continue
            for i, (fid, val, units, raw) in enumerate(fields, 1):
                raw_s = f"  [id={fid:02x} a={raw[0]:3d} b={raw[1]:3d}]" if raw else ""
                if g in (69, 70) and fid in (0x08, 0x10, 0x25):
                    print(f"  {i}: {render_bitfield(val)} (bitfield){raw_s}")
                elif isinstance(val, float):
                    print(f"  {i}: {val:.1f} {units}{raw_s}")
                else:
                    print(f"  {i}: {val} {units}{raw_s}")
    finally:
        try:
            kwp.disconnect()
        except Exception:
            pass
        try:
            dev.release_interface(0)
        finally:
            dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
