# Springtide

**A regeneration and fault monitor for pre-CAN VAG diesel cars** — built and
field-tested on a Seat Ibiza 6L 1.4 TDI (engine code BMS, EDC17-class ECU on
a K-line), the generation of diesel that no modern OBD app can talk to about
its particle filter.

Springtide answers three questions a DPF driver actually has, in the car,
with voice:

1. *Is a regeneration running right now?* — "Regeneration initiated. Continue driving."
2. *Did it finish, and over how many km?* — "Regeneration complete after 9 minutes and 5 kilometers."
3. *Is something wrong with the engine?* — "Malfunction registered. Fault code P zero six seven zero."

…and lets the **driver** decide whether to wipe the fault memory, with a
button press. The monitor never writes to the car on its own.

## Why this exists

Pre-2008ish VAG diesels expose their interesting data (soot load, DPF
temperatures, regen counters) as **KW1281/KWP2000 measuring blocks over a
single K-line wire** — not as OBD-II PIDs, not over CAN. Consequences:

- Every ELM327-based phone app (Torque, Car Scanner, …) sees only the small
  "legislated" OBD subset: coolant, RPM, speed, stored fault codes.
- No wireless adapter on the market reads VAG measuring blocks on these
  cars. (The Android "VAG DPF" app is CAN-era only.)
- A laptop + VCDS cable reads everything, but you don't want a laptop in
  the car for every drive.

Springtide bridges that with a **two-way approach**, both running on one
rooted Android phone in the car (any phone with Termux works; it does not
need a SIM):

| | **Way 1: BLE dongle (zero hardware)** | **Way 2: K-line direct (KW1281)** |
|---|---|---|
| Hardware | ~10 € Bluetooth LE ELM327 dongle | KKL USB cable (dev) or ~15 € ESP32 node (permanent) |
| Data | Legislated OBD: coolant, speed, RPM, DTCs | Real soot %, DPF temps, regen counters, injection data |
| Detection | Coolant signature of a regen (92 °C rule) | ECU-native regen flags + soot load |
| Status | **Live, field-tested over months of drives** | KKL dev path working; ESP32 node documented for build |
| Reliability | Independent of the ECU's block layer | Ground truth; cross-checks Way 1 |

Way 1 is the one you can build in an afternoon. Way 2 is the deep-data
upgrade; its USB dev path is working code in `tools/`, and its permanent
form is a documented ESP32 build (see `docs/esp32-node.md`).

---

## Way 1 — the coolant method (BLE dongle)

### The physics

On this engine family a thermal DPF regeneration is visible in **coolant
temperature**: the normal operating plateau is 89–91 °C, and while the ECU
is burning soot off the filter the plateau moves to **92–93 °C** for the
duration (typically 10–25 min). Springtide watches for exactly that:

- **92.0 °C absolute threshold** — not "temperature rose": a parked warm-up
  or a slow climb must never trigger it. (A baseline+delta rule produced a
  45-minute false positive in field testing; the absolute rule replaced
  it.)
- **180 s candidate confirm** — three minutes ≥ 92 °C before the voice
  announcement fires.
- **Grace dips < 120 s tolerated** — the signal jitters 91↔92↔93 with the
  sensor's 1 °C granularity; a brief dip must not restart the clock.
- **End = below 92 °C for 120 s** → announcement with duration and
  integrated km.
- **Engine off mid-regen** → "interrupted, it will resume next drive"
  (these ECUs resume regens; the detector says so honestly).
- **90-minute safety cap** and cold-engine reset (every drive starts with
  a fresh baseline; a cooling engine's 75–79 °C readings must not poison
  the next warm-up).

All tunables live in `config.json` and are documented inline.

### The stack

```
car K-line ── ELM327 BLE clone ──(Bluetooth LE GATT)── BridgeService APK
                                                        │ local TCP 127.0.0.1:35000
                                                        ▼
                                              springtide_daemon.py (Termux)
                                              ├── coolant regen detector
                                              ├── DTC poll + voice announce
                                              ├── driver-commanded DTC clear
                                              ├── CSV logs + live.json
                                              └── Announcer (offline WAV voice)
```

The bridge APK exists because **Android Bluetooth is app-process-only** —
Termux/python can never open a BLE socket, no matter what permissions or
root you have. The APK is a deliberately dumb ~300-line GATT pipe: scan,
connect, pump bytes to a local TCP port. All logic stays in Python,
editable over SSH.

### Voice

Alerts are pre-generated WAV fragments (Kokoro TTS, 24 kHz mono, run
through a broadcast-loud compressor/limiter chain for a diesel cabin),
stitched at playback time in pure Python and played through the phone
speaker. Fully offline — the voice layer needs no network, ever. Three
personalities ship (neutral, "Seven", "KITT"), each with 177 fragments
including spelled-out fault codes.

### Fault codes (DTCs)

Every 60 s the daemon reads the stored fault memory (OBD mode 03). When a
**new** code appears, the car announces it — a failed glow-plug module
arrives as *"Malfunction registered. Fault code P zero six seven zero."*

Clearing is **always the driver's decision**:

- On the in-car status screen (`dash.py`): press **`c`** — the daemon
  sends mode 04, re-reads the memory to verify, and confirms by voice.
- Over SSH: `python3 springtide_daemon.py --clear-dtc` queues the same
  request; it is honored at the next ignition-on.

A request made as you park is honored at the next drive start. Failed
clears are parked, never retried in a loop. Every seen/cleared code is
logged to `springtide_dtc_YYYYMMDD.csv`.

### Build it (an afternoon)

1. Any rooted Android phone with Termux + Termux:Boot (a retired flagship
   is perfect — it lives in the car permanently).
2. A Bluetooth-LE ELM327 dongle (~10 €). **Caveat tier list**: cheap
   v2.1 BLE clones work but have quirks (see `docs/hardware-notes.md`);
   a v1.5 PIC clone or an OBDLink-class device is the reliable tier.
3. Build and sideload the bridge APK (`build.sh`, needs the Android SDK) —
   or write your own: it is a tiny, auditable pipe.
4. Copy the repo to `~/dpf/` on the phone, `./run.sh`, add the boot
   autostart hook, done.
5. Optional: `dash.py` gives a curses status screen on the phone.

---

## Way 2 — K-line direct (KW1281 measuring blocks)

Real soot load, DPF inlet/outlet temperatures, regen counters, injection
quantities — the data the ECU actually computes. Two paths:

### Dev path: KKL USB cable (working today)

A **FT232RL-based KKL ("VAG-COM 409") cable** on the phone via USB-OTG.
`tools/ftdi_usb.py` is a pure-stdlib usbdevfs driver for the FT232R (it
synthesizes the 5-baud slow-init on BREAK), `tools/kwp1281.py` speaks
KW1281 (connect, read measuring blocks, decode), `tools/kwp1281_probe.py`
reads the DPF block set in one shot. Mock-ECU tested; see
`docs/kkl-usb.md` for the session recipe and an honest warning about clone
cable quality.

### Permanent: the ESP32 node (documented build)

A ~15 € always-in-the-car node: ESP32 + L9637D K-line transceiver wired to
the OBD port, powered from an ignition-switched 12 V feed, speaking
KW1281 and streaming decoded blocks to the phone over WiFi. Full parts
list, wiring, enclosure and firmware plan in **`docs/esp32-node.md`** —
including why the node powers from the ignition-switched socket feed
rather than the always-hot OBD pin (zero parasitic drain, and the buck's
voltage display doubles as a "system on" lamp).

---

## Repository layout

```
springtide_daemon.py     the daemon: detector, DTC layer, announcer, logs
dash.py                  in-car curses status screen ('c' clears faults)
app/src/.../BridgeService.java   BLE→TCP bridge (the only Android code)
build.sh                 bridge APK build (pins working d8 from build-tools 35)
run.sh                   supervisor loop (respawn on crash)
config.json              all tunables
tools/
  test_dtc.py            hardware-free DTC-layer tests
  ftdi_usb.py            FT232R userland driver (usbdevfs, no kernel module)
  test_ftdi_usb.py       19 hardware-free driver tests
  kwp1281.py             KW1281 protocol layer
  kwp1281_probe.py       on-car probe: ident + DPF measuring blocks
  test_kwp1281.py        FakeECU protocol harness
  fuel_report.py         trip fuel/100km from the fuel CSV
  gen: voice/gen_en.py   Kokoro fragment generator (loudness chain)
docs/                    hardware notes, KKL guide, ESP32 node build
```

## Field-test log (honest)

- **Drive 1**: pipeline held 47 min, voice fired, but the first detection
  rule false-positived on a normal 89–91 °C plateau → replaced with the
  absolute 92 °C rule.
- **Drive 2** (absolute rule): 46 km, zero announcements, peak exactly
  91 °C — correct silence.
- **Drive 3**: a real regen pinned coolant at 92–93 °C for 15 min, but the
  unbroken-180 s confirm never saw a long enough streak → grace-dip
  confirm added; replayed verdict: 9.2 min / 5.2 km correctly announced.
- **Glow-plug module fault (P0670)**: announced by voice, cleared by the
  driver's button press, verified clean by re-read.

The detection is tuned against real drives, not simulations; replayed CSVs
of these drives are the regression set for any future rule change.

## License

MIT for all code. Voice fragments are generated with
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0).

**Not included:** VCDS/Ross-Tech label data (proprietary — decode your own
with `tools/clb_decode.py` if you have licensed access) and any data
identifying the original test car or its owners.
