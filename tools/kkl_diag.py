#!/usr/bin/env python3
"""K-line contact diagnostic for the KKL cable: idle listen + 5-baud address sweep.

Decision table (idle 3 s listen):
  drift noise (~4 B/s, repeating pattern)  -> line FLOATS: K-line contact to
                                              the car is not made -> reseat plug
  silence                                   -> line held high: contact likely
                                              good -> watch the init sweep
Then slow-init a few VAG addresses; any 0x55 lead byte = SYNC.

Run on the phone: su -c '/data/data/com.termux/files/usr/bin/python3 \
                    /data/data/com.termux/files/home/dpf/kkl_diag.py'
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402


def main():
    path = F.find_ft232r()
    print(f"device: {path}")
    dev = F.UsbDevice(path)
    dev.claim_interface(0)
    ftdi = F.Ftdi232R(dev)
    port = F.KKLPort(ftdi)
    port.open_10400()

    t0 = time.monotonic()
    noise = bytearray()
    while time.monotonic() - t0 < 3.0:
        noise += port.read(64, 200)
    if noise:
        print(f"idle 3 s: {len(noise)} noise bytes: {bytes(noise[:24]).hex()}")
        print("VERDICT: line FLOATS — no pullup is holding it: reseat the OBD plug.")
    else:
        print("idle 3 s: SILENT — line held high, contact looks good.")

    for addr in (0x01, 0x17, 0x56):
        got = port.slow_init(addr=addr, sync_timeout_ms=1200)
        if got.startswith(b"\x55"):
            tag = "SYNC"
        elif got:
            tag = "noise"
        else:
            tag = "silent"
        extra = got.hex() if got else ""
        print(f"init addr 0x{addr:02x}: {tag} {extra}")

    try:
        dev.release_interface(0)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
