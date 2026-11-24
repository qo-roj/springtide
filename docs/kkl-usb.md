# KKL USB cable on Android — the KW1281 dev path

The quick way to real measuring blocks without waiting for the ESP32
node: a KKL ("VAG-COM 409 clone") USB cable on the phone via USB-OTG,
driven entirely from userspace.

## What works

- `tools/ftdi_usb.py` — pure-stdlib **usbdevfs driver for the FT232R**
  UART in these cables. No kernel module needed (the phone kernel has
  none), no root in steady state: claim the device, set the divisor
  (10400 baud works via the FTDI 232BM fractional divisor:
  `0x4120` → 10398.6 baud, −0.013 %), and synthesize the **5-baud
  slow-init on BREAK** (FTDI_SIO_SET_DATA bit 14).
- `tools/kwp1281.py` — the KW1281 client: connect (addr `0x01` → `55 01
  8A` → `75`), read measuring blocks, decode fields; FakeECU-tested.
- `tools/kwp1281_probe.py` — one-shot on-car probe: identification plus
  the DPF block set (067–075: temperatures, soot load coefficient, regen
  bits/counters, distance since last regen).

Session recipe and block meanings are documented inline in the tools.

## Cable quality — read this before ordering

The market is flooded with defective KKL clones. Field experience with
one:

- A clone whose analog TX/bias stage **oscillated the K-line at ~520 Hz**
  the moment it was powered, saturating the bus with garbage in every
  DTR/RTS state — no ECU can answer over that. The failure mode worsened
  as the cable warmed. Diagnosis: idle-listen tests plus a DTR/RTS matrix
  (the noise tracked our modem lines live → the fault was in the cable,
  not the car). The car was exonerated by two independent adapters.
- Separate, known failure report class: **unpowered clones left in an
  always-hot OBD port can heat two resistors** — never leave one plugged
  long-term without USB power.

Buying advice:

1. Order an **FT232RL-based** cable (enumerates as "FT232R USB UART") —
   the driver in this repo is FTDI-specific. A CH340-based clone means
   writing a second driver layer (same usbdevfs pattern, CH340 vendor
   requests — doable, but order FTDI anyway).
2. When it arrives: idle line MUST be quiet. Saturated noise or a slow
   drifting pattern = RMA it, stop there.
3. Test at the car before trusting a drive to it: 5-baud init must return
  `55 01 8A`.

## Why this is a dev path, not the permanent install

USB-OTG host mode disables charging on most phones, the cable occupies
the only USB port, and a dangling OTG adapter in a moving car is a
contact-fault generator. For the permanent in-car reader see
`docs/esp32-node.md` — the KKL stays a convenient bench/diagnostic tool.
