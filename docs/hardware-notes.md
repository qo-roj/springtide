# Hardware notes — dongles, phones, and the Android Bluetooth wall

## ELM327 dongle tier list (field-observed)

| Tier | Example | Verdict |
|---|---|---|
| Cheap v2.1 BLE clones (~10 €) | NORAUTO BT36044 class | Work, but expect quirks: firmware behavior changes across power cycles, single-client GATT (the dongle stops advertising while any app holds it), possible K-line contact faults from the OBD plug fit. Fine for Way 1. |
| v1.5 PIC clones | classic "v1.5" | The reliable tier for ELM work. |
| OBDLink / Vgate iCar2 / Carista | — | The recommended tier if you want zero hardware drama. |

Quirks worth knowing before you debug the wrong thing:

- **Single client**: while any app holds the GATT connection, the dongle
  does not advertise. To hand it back to the bridge: kill the other app
  and turn that phone's Bluetooth off — the dongle resumes advertising by
  itself. A "dead dongle" that another app can still read is not dead.
- **Clone firmware is not stable across resets**: an auto-search init that
  worked yesterday can break after a power cycle; explicit protocol init
  (`ATSP 4`, ISO 9141-2) proved durable where `ATSP 0` auto and `ATSP 5`
  both failed at different times. If the ECU goes silent, re-probe init
  variants manually before suspecting hardware.
- **K-line contact faults happen at the OBD port**: a dongle that reads
  flawlessly parked but loses the ECU once the car moves is usually a
  seat problem — reseat the plug. 
- **Many VAG OBD ports are always-hot**: a plugged dongle draws 30–50 mA
  indefinitely. Unplug for multi-week parking.
- **BLE needs location services enabled** on Android for unfiltered scans
  (pre-Android-12 behavior); the bridge scans by name OR saved MAC, and
  falls back to a background direct-connect to the MAC, which survives
  advertising failures entirely.

## The Android Bluetooth wall (why the bridge APK exists)

Termux/shell/python **cannot** open Bluetooth sockets on Android — the
entire BT stack lives in app processes (JVM). No root, no wrapper, no
python library changes this; `pyjnius` in Termux fails with "no in-process
JVM". Scanner apps work because they are Java apps. Therefore the
architecture: a tiny foreground-service APK owns BLE and pipes bytes over
local TCP; Python owns all logic. Fix BT problems in the bridge or not at
all — never try to fix them from Termux.

## Phone requirements (Way 1)

- Rooted Android (for `svc power stayon`, headless `su -c` helpers, and
  sideloading) running Termux + Termux:Boot
- Bluetooth LE, location services on
- Permanent power from the car (12 V socket USB charger) — the screen-off
  Doze defenses (foreground service + wakelock + battery-optimization
  exemption for Termux) are required, not optional
- No SIM needed: detection, voice and logging are fully offline; remote
  access (Tailscale/SSH) is purely an operator convenience

## Voice loudness (why the fragments sound "radio-ready")

Raw TTS output is ~8 LU too quiet over a diesel at 90 km/h. The generator
(`voice/gen_en.py`) runs every fragment through 10:1 compression at
−24 dB with +18 dB makeup and a limiter — loudness by construction,
uniform across fragments so stitched numbers match sentence loudness.
Peak normalization and two-pass loudnorm both fail here: normalizer gain
is capped by the true-peak ceiling, and peak-maxed input leaves no
headroom. Never ship raw TTS output.
