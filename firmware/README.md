# Springtide ESP32 node firmware

Permanent in-car K-line node: an ESP32 (D1 mini32 class) with a K-line
transceiver polls the ECU over KW1281 and streams decoded measuring
blocks as JSON lines to the monitor phone over WiFi.

## Layout

- `kw1281/` — portable KW1281 client core (plain C, no Arduino
  dependencies). Direct C port of the FakeECU-tested python layer
  (`tools/kwp1281.py`): same frame codec, same complement-ACK flow
  control, same jazdw field-decode table.
- `tests/` — host tests (`gcc`, no hardware). `fake_ecu.c` models the
  single-wire bus strictly: bus echo pushed in front, complement ACKs
  validated in order, ident blocks staged on the tester's ACK-block
  progression like a real ECU. A green run proves the interleave
  discipline, not just the happy path.
- `springtide_node/` — the Arduino sketch (ESP32, UART2 + WiFi uplink).
  `node_config.h` carries PLACEHOLDER values — fill at flash time,
  never commit real ones.
- `build.sh` — syncs the core into the sketch folder (the Arduino
  toolchain only compiles the sketch dir) and compiles.

## Build & test

```sh
firmware/tests/run.sh                     # host tests (gcc)
firmware/build.sh                         # compile for esp32doit-devkit-v1
firmware/build.sh --upload PORT           # not yet: needs the wired node
```

`arduino-cli` + the `esp32:esp32` core are required; see
`docs/esp32-node.md` for the hardware the firmware targets.

## Poll set

Per the decoded VCDS label data for this ECU (BMS, soft 0477):

| Group | Fields | Meaning |
|---|---|---|
| 001 | 4 | coolant cross-check + rpm/injection context |
| 068 | 1-3 | DPF soot load % (regen while driving >50%, limp >75%, prohibited >90%) + carbon mass g |
| 069 | 1, 4 | regen status bitfields (bit0 interrupted / active / enhanced) |
| 070 | 1-4 | regen bits, regen time total, failed regens (spec max 4), successful regens |
| 073 | 1-3 | consumption / km / time since last successful regeneration |

Coolant cross-check: the 92 °C absolute rule stays authoritative on the
phone-side daemon; the node's group-001 coolant is the independent
ground-truth lane.

## Uplink format

One JSON object per line over TCP (`UPLINK_HOST:UPLINK_PORT`):

```json
{"g":68,"f":[{"i":20,"v":49.0,"u":"%"},{"i":54,"v":12.5,"u":"count"}]}
{"g":68,"err":-6,"e":"unexpected title"}
```

`i` is the jazdw field id, `v` the decoded value, `u` the units;
string fields arrive as `{"i":63,"s":"..."}`. Errors are reported, never
swallowed — the receiver (the daemon's node listener) decides what to
do with them. No buffering when the link is down in v1: the next cycle
carries fresh values.

## 5-baud wake

Bit-banged on the TX pin, identical train to the proven python/FTDI
implementation: start(0) + 7 data bits LSB-first + parity(1 XOR data
bits) + stop(1), 200 ms per bit; RX flushed before the sync read (the
wake train echoes back as garbage at 10400 baud).

## Constraints kept

- No KKL cable anywhere in this chain (USB needs a host; the node has
  its own transceiver on a GPIO).
- No Bluetooth to the phone (Android BT is app-process-only); uplink
  is WiFi.
- The transceiver is a dumb UART part — TXD/RXD only — so the same
  firmware runs against either the L9637D or the TLIN1027.
- Ignition-switched 12 V power: the node is dark while parked, so no
  session-end packet exists — the receiver infers drive end from data
  silence.
