# The ESP32 K-line node — permanent in-car KW1281 reader

The permanent form of Way 2: a ~15 € module that lives in the car, talks
KW1281 to the ECU on the K-line, and streams decoded measuring blocks
(soot load, DPF temperatures, regen flags/counters, injection data) to the
phone over WiFi. No cable in the driver's footwell, no laptop, no
battery-draining USB-OTG sessions.

This document is the full build: parts, wiring, enclosure, firmware plan,
and the reasoning behind the non-obvious choices.

## Why not just leave a KKL cable plugged in?

Three reasons, all learned the hard way:

1. **USB-OTG host mode disables charging** on most phones — a permanent
   cable occupies the only USB port and the phone runs on battery alone.
2. **An unpowered KKL clone left in the OBD port can heat up** — this
   cable family has known "two hot resistors" failure reports when it sits
   in the always-hot pin 16 without USB power.
3. **Connectors wear** in a moving car; a dangling OTG adapter is a
   contact-fault generator.

The ESP32 replaces the PC-oriented USB chain entirely: the K-line wire
goes straight to a microcontroller GPIO through a proper transceiver.

## Parts list (~15 € total)

| Part | ~Price | Notes |
|---|---|---|
| ESP32 D1 Mini (or any ESP32 devkit) | 3 € | WiFi + BLE, deep-sleep capable |
| L9637D K-line transceiver (SO-8) | 2 € | The automotive-standard K-line driver; SO-8 adapter board is handy |
| OBD2 plug pigtail or sacrificial OBD extension cable | 3–4 € | You need pins 4/5 (GND), 7 (K-line), 16 (bat+) — cut the rest |
| 12 V→5 V buck converter **with voltage display** | 4 € | The display is not a gimmick — it doubles as the "system on" lamp (see Power) |
| Perfboard, pin headers, hookup wire, heatshrink | 2 € | |
| Micro USB or USB-C panel jack (optional) | 1 € | Only for firmware flashing without opening the box |
| Small ABS project box or 3D-printed enclosure | 0–2 € | Needs: display window, flash-port cutout, cable pass-throughs, vents |

Tools: soldering iron, multimeter, crimp pliers optional. A 3D printer is
nice for the enclosure but a small project box drills just as well.

## Wiring

```
OBD pin 16 (battery +12 V) ──or── ignition-switched 12 V feed   ──► buck IN+
OBD pins 4/5 (GND) ───────────────────────────────────────────► buck IN−,
                                                               node GND
buck OUT 5 V ──► ESP32 5V/VIN pin
buck OUT GND ─► common GND rail (with ESP32 GND — one ground domain)

OBD pin 7 (K-line) ──► L9637D K-line pin (pin 6, the bus side)
L9637D TXO (pin 5)  ──► 2.2 kΩ/3.3 kΩ divider ──► ESP32 RX (3.3 V logic!)
ESP32 TX ──► L9637D RXI (pin 1) — 3.3 V into a 5 V part is within spec
L9637D VCC (pin 2) ──► 5 V rail; L9637D GND (pin 3) ──► GND
Optional: 1 kΩ series resistor on the K-line lead
```

Two details that matter:

- **Level shifting on RX**: the L9637D is a 5 V part; its output must be
  divided down before it meets an ESP32 GPIO (3.3 V max). The ESP32's 3.3 V
  TX into the L9637D input is fine as-is.
- **The K-line idle state is pulled high** (there is already a pull-up in
  the car). Do not add a strong pull-up of your own; a healthy K-line is
  idle-high and quiet. A line that idles low or oscillates is a wiring
  fault, not protocol behavior.

### Power: ignition-switched, NOT the always-hot OBD pin

Power the node from an **ignition-switched 12 V feed** (the 12 V accessory
socket feed is ideal and close to the OBD port on the target car family):

- **Zero parasitic drain** when parked — the node is simply off. Soot only
  accumulates with the engine running, so nothing is lost.
- No firmware deep-sleep logic needed for drain reasons.
- **The buck's voltage display becomes the status lamp**: dark = parked,
  showing battery voltage = system on. One glance answers "is it
  powered".
- The always-hot OBD pin 16 stays reserved for plugging in adapters.

The module powers down with the ECU, so there is no protocol-level
goodbye — the phone daemon already infers drive-end from data silence.

### Assembly order

1. Breadboard first: transceiver + ESP32, K-line and 12 V from a bench
   supply (or the car), flash firmware, confirm `55 01 8A` sync (see
   Firmware).
2. Only then solder the perfboard: keep the K-line lead short, twist it
   with its ground return, keep the buck away from the transceiver input.
3. Test the board in the car before boxing it: OBD pin 7 → L9637D, GND,
   ignition-switched feed → buck; verify the display lights with
   ignition.
4. Enclosure: window for the buck display, cutout for the flash port,
   cable glands/heatshrink for the three wires (12 V, GND, K-line — or one
   3-wire OBD pigtail), vents for the buck's warmth.
5. Mount under the steering column near the 12 V socket — short runs to
   both the socket feed and the OBD port.

## Firmware plan

The protocol is KW1281 over K-line at 10400 baud, 5-baud slow-init with
address 0x01 (engine ECU):

1. **Sync**: send address byte 0x01 at 5 baud → ECU answers
   `55 01 8A` → tester sends `75` (~30 ms after the keyword byte) →
   ECU streams identification blocks; ACK each (the complement handshake
   IS the flow control) until the ECU sends its own ACK.
2. **Read blocks**: request group N (e.g. 068 for soot load, 069/070 for
   regen status bits and counters, 073 for distance since last regen),
   decode the 3×4-byte fields, repeat.
3. **Uplink**: connect the ESP32 to the phone's WiFi hotspot and stream
   JSON lines over TCP; the daemon's `live.json` layer consumes them as
   the primary detection source with the coolant layer as cross-check.

Reference implementations worth reading before writing a line:

- `mnaberez/vwradio` — `kwp1281.c` is a complete, plain-C KW1281 client
  (connect + read blocks); it ports to ESP32 almost verbatim.
- This repo's `tools/kwp1281.py` — the same protocol in Python with the
  block/echo/complement mechanics spelled out and a FakeECU test harness;
  use it to validate your understanding against a known-good state machine
  before porting to C.
- This repo's `tools/ftdi_usb.py` — the 5-baud slow-init timing and VAG
  bit format (start + 7 data bits LSB + parity + stop) in executable
  form.

**Test against the FakeECU harness first** (mock ECU simulating echoes,
complements and block ends). Protocol-layer bugs that "mostly work" will
lock the K-line bus against your own reads; the harness catches the
interleave-discipline mistakes in seconds instead of at the car.

### Verification at the car (checklist)

- [ ] Idle K-line is quiet (oscilloscope or the diag tools): noise = wiring fault
- [ ] 5-baud init returns `55 01 8A`
- [ ] Identification blocks name your ECU (part no / soft version)
- [ ] Block 001 coolant matches the dashboard/gauge
- [ ] DPF blocks (067–075 on the test car's ECU) decode to plausible values
- [ ] Power-cycle test: ignition off → node dark; ignition on → sync < 5 s

## What this node does NOT do

- No writes. Like the daemon, the node is read-only; fault-memory clears
  stay on the legislated OBD layer, driver-commanded.
- No CAN. These cars have no powertrain CAN with DPF data on it; the
  K-line IS the only path.
- Not a replacement for the coolant method: the coolant rule stays live as
  an independent cross-check. Two sensors that agree are worth more than
  either alone.
