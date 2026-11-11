#!/usr/bin/env python3
"""DTR/RTS gating matrix for the KKL clone + init attempts per state.

Many KKL 409 clones use DTR/RTS to gate the K-line transceiver or the RX
bias network. The springtide port init asserts both high — if that is what
is disturbing the line, one of the four states will silence it.

Run on the phone: su -c '/data/data/com.termux/files/usr/bin/python3 \
                    /data/data/com.termux/files/home/dpf/kkl_diag2.py'
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402


def listen(ftdi, secs):
    t0 = time.monotonic()
    n = 0
    head = bytearray()
    while time.monotonic() - t0 < secs:
        got = ftdi.read(64, 200)
        n += len(got)
        if len(head) < 16:
            head += got
    return n, bytes(head[:16])


def main():
    dev = F.UsbDevice(F.find_ft232r())
    dev.claim_interface(0)
    ftdi = F.Ftdi232R(dev)
    port = F.KKLPort(ftdi)
    port.open_10400()

    print("=== idle listen per DTR/RTS state ===")
    for dtr, rts in [(1, 1), (0, 0), (1, 0), (0, 1)]:
        ftdi.set_dtr_rts(bool(dtr), bool(rts))
        ftdi.purge()
        n, head = listen(ftdi, 1.5)
        rate = n / 1.5
        state = "QUIET" if n == 0 else ("SATURATED" if rate > 300 else "drift")
        print(f"DTR={dtr} RTS={rts}: {n:4d} bytes/1.5 s ({rate:6.0f} B/s) {state:9s} head={head.hex()}")

    print("=== slow init addr 0x01 per DTR/RTS state ===")
    for dtr, rts in [(1, 1), (0, 0), (1, 0), (0, 1)]:
        ftdi.set_dtr_rts(bool(dtr), bool(rts))
        got = port.slow_init(addr=0x01, sync_timeout_ms=1200)
        if got.startswith(b"\x55"):
            tag = "SYNC"
        elif got:
            tag = "noise"
        else:
            tag = "silent"
        extra = got.hex() if got else ""
        print(f"init 0x01 DTR={dtr} RTS={rts}: {tag} {extra}")

    try:
        dev.release_interface(0)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
