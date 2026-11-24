#!/usr/bin/env python3
"""On-device smoke test for ftdi_usb.py — the real FT232R in the KKL cable.

No car needed: every step talks to the chip itself (enumeration, claim,
vendor requests both directions, divisor values, break toggle, TX,
RX-timeout, the full 5-baud init sequence, EEPROM). Steps that require
the ECU on the K-line are EXPECTED to time out and marked accordingly.

Run on the phone (root, Termux python):
  su -c '/data/data/com.termux/files/usr/bin/python3 \
         /data/data/com.termux/files/home/dpf/ftdi_smoke.py'
or unprivileged after: su -c 'chmod 666 /dev/bus/usb/001/002'
"""

import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ftdi_usb as F  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def step(name, expected_note=""):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper():
            t0 = time.monotonic()
            try:
                detail = fn()
                results.append((name, PASS, detail, time.monotonic() - t0))
                print(f"[PASS] {name}: {detail}")
            except Exception as e:
                results.append((name, FAIL, str(e), time.monotonic() - t0))
                print(f"[FAIL] {name}: {type(e).__name__}: {e}"
                      + (f" ({expected_note})" if expected_note else ""))
        return wrapper
    return deco


ftdi = None
port = None
dev = None


@step("discover FT232R")
def s01():
    global dev
    path = F.find_ft232r()
    dev = F.UsbDevice(path)
    return path


@step("usbfs capabilities + connect info")
def s02():
    caps = dev.get_capabilities()
    ci = dev.connect_info()
    return f"caps=0x{caps:x} devnum={ci['devnum']}"


@step("descriptors")
def s03():
    dd = dev.device_descriptor()
    ser = dev.string_descriptor(dd["iSerialNumber"])
    prod = dev.string_descriptor(dd["iProduct"])
    return (f"{dd['idVendor']:04x}:{dd['idProduct']:04x} '{prod}' "
            f"serial={ser} bcdDevice=0x{dd['bcdDevice']:04x}")


@step("claim interface 0")
def s04():
    dev.claim_interface(0)
    return "claimed"


@step("FTDI init (sio reset, purge, flow, event chars, DTR|RTS)")
def s05():
    global ftdi
    ftdi = F.Ftdi232R(dev)
    ftdi.purge()
    ftdi.set_dtr_rts()
    ftdi.set_flow_none()
    ftdi.disable_event_chars()
    return f"endpoints IN=0x{ftdi.ep_in:02x} OUT=0x{ftdi.ep_out:02x} mps={ftdi.max_packet_size}"


@step("latency set/get round-trip")
def s06():
    ftdi.set_latency(2)
    got = ftdi.get_latency()
    assert got == 2, f"read back {got}"
    return "set 2 ms, read back 2 ms"


@step("baud divisors 9600 + 10400 (kernel-exact)")
def s07():
    r96 = ftdi.set_baud(9600)
    assert r96 == (0x4138, 0), f"9600 -> {tuple(hex(x) for x in r96)}"
    r104 = ftdi.set_baud(10400)
    assert r104 == (0x4120, 0), f"10400 -> {tuple(hex(x) for x in r104)}"
    eff = F.bm_effective_baud(10400)
    return f"9600=0x4138/0, 10400=0x4120/0 ({eff:.1f} baud effective)"


@step("line 8N1 + break toggle")
def s08():
    ftdi.set_line()
    t0 = time.monotonic()
    ftdi.set_break(True)
    ftdi.set_break(False)
    dt = (time.monotonic() - t0) * 1000
    assert dt < 50, f"break toggle pair took {dt:.1f} ms"
    return f"0x0008 <-> 0x4008, toggle pair {dt:.1f} ms"


@step("TX at 10400 (chip accepts)")
def s09():
    n = ftdi.write(b"\x55", timeout_ms=1000)
    assert n == 1, f"wrote {n}"
    return "1 byte queued to TX endpoint"


@step("RX read path (silence or floating-line noise — both prove RX)")
def s10():
    t0 = time.monotonic()
    got = ftdi.read(16, 400)
    dt = (time.monotonic() - t0) * 1000
    if got == b"":
        assert 300 <= dt <= 1500, f"timeout after {dt:.0f} ms"
        return f"clean timeout after {dt:.0f} ms — RX up, line quiet"
    return (f"RX delivered {got.hex()} in {dt:.0f} ms — floating K-line noise "
            f"(OBD end dangling), RX path live")


@step("K-line port init")
def s11():
    global port
    port = F.KKLPort(ftdi)
    v = port.open_10400()
    return f"configured, request={tuple(hex(x) for x in v[0])}"


@step("full 5-baud slow init sequence")
def s12():
    t0 = time.monotonic()
    got = port.slow_init(addr=0x01)
    dt = time.monotonic() - t0
    assert 1.9 <= dt <= 3.8, f"sequence took {dt:.2f} s"
    if got.startswith(b"\x55"):
        return f"ECU SYNC RECEIVED: {got.hex()} — key bytes in hand!"
    if got == b"":
        return f"2.0 s pulse train sent, no sync byte within timeout ({dt:.2f} s)"
    return (f"pulse train sent; {len(got)} noise bytes ({got.hex()}) — "
            f"dangling OBD end, expected on a floating line")


@step("EEPROM read (serial region, non-fatal)")
def s13():
    try:
        words = [ftdi.read_eeprom_word(a) for a in range(0, 10)]
        return " ".join(f"{w:04x}" for w in words)
    except Exception as e:
        print(f"       (eeprom read failed, non-fatal: {e})")
        return "skipped"


@step("release + close")
def s14():
    try:
        dev.release_interface(0)
    finally:
        dev.close()
    return "released, closed"


def main():
    print(f"ftdi_smoke on {sys.version.split()[0]}, pid {os.getpid()}")
    steps = [v for k, v in list(globals().items()) if k.startswith("s") and k[1:3].isdigit()]
    steps.sort(key=lambda fn: fn.__name__)
    for fn in steps:
        fn()
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed")
    if failed:
        print("FAILED:", ", ".join(r[0] for r in failed))
        return 1
    print("ALL GREEN — transport validated against the real FT232R. "
          "Next: car ignition on, run the KW1281 probe through KKLPort.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
