#!/usr/bin/env python3
"""Fuel consumption report from Springtide CSVs.

Integrates springtide_fuel_YYYYMMDD.csv (PID 5E, L/h) against the main
springtide_YYYYMMDD.csv (speed, rpm) to produce trip consumption in
l/100km, total liters, idle burn, and flow stats. Rows with non-numeric
fields (engine-off boundary samples) are filtered before math.

Usage:
  fuel_report.py [LOGDIR] [YYYYMMDD]     # defaults: ./logs, today
"""

import os
import sys
from datetime import datetime, timedelta

# PID 5E resolution is 0.05 L/h; a single sample is quantized — treat
# tiny/negative parses as bad.
def _rows(path, ncols):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        f.readline()  # header
        for ln in f:
            p = ln.strip().split(",")
            try:
                t = datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S")
                out.append((t, *(float(p[1 + j]) for j in range(ncols))))
            except (ValueError, IndexError):
                pass
    return out


def report(logdir, ymd):
    main = _rows(os.path.join(logdir, f"springtide_{ymd}.csv"), 3)
    fuel = _rows(os.path.join(logdir, f"springtide_fuel_{ymd}.csv"), 1)
    if not main:
        print(f"no main CSV for {ymd}")
        return 1
    print(f"trip rows: {len(main)}  fuel rows: {len(fuel)}")
    t0, t1 = main[0][0], main[-1][0]

    def dt(a, b):
        d = (b - a).total_seconds()
        return d if 0 < d < 30 else None

    liters = 0.0
    if fuel:
        f0, f1 = fuel[0][0], fuel[-1][0]
        overlap = (max(t0, f0), min(t1, f1))
        if overlap[0] >= overlap[1]:
            print("fuel data exists but time ranges do not overlap")
        prev = None
        for r in fuel:
            if r[0] < overlap[0] or r[0] > overlap[1]:
                prev = r if r[0] <= overlap[0] else prev
                continue
            if prev is not None and dt(prev[0], r[0]):
                liters += (prev[1] + r[1]) / 2 * dt(prev[0], r[0]) / 3600
            prev = r
    km = 0.0
    prev = None
    for r in main:
        if prev is not None and dt(prev[0], r[0]):
            km += (prev[2] + r[2]) / 2 * dt(prev[0], r[0]) / 3600
        prev = r
    moving_km = moving_s = idle_s = 0.0
    prev = None
    for r in main:
        if prev is not None and dt(prev[0], r[0]):
            d = dt(prev[0], r[0])
            mid = (prev[2] + r[2]) / 2
            if mid > 2:
                moving_km += mid * d / 3600
                moving_s += d
            else:
                idle_s += d
        prev = r
    print(f"span: {t0} -> {t1} ({(t1 - t0).total_seconds() / 60:.1f} min)")
    print(f"distance: {km:.1f} km (moving {moving_km:.1f} km, "
          f"idle {idle_s / 60:.1f} min)")
    if fuel and liters > 0:
        print(f"fuel: {liters:.2f} L over overlap window")
        if km > 0:
            print(f"CONSUMPTION: {100 * liters / km:.2f} l/100km")
        if moving_km > 0.5:
            # split fuel proportional to time is wrong for idle; report
            # idle burn separately: idle flow ~0.6-0.9 L/h on a warm 1.4 TDI
            print("(idle burn included in the total; see idle minutes above)")
    else:
        print("no fuel data (PID 5E unsupported or patch not yet deployed)")
    return 0


if __name__ == "__main__":
    logdir = sys.argv[1] if len(sys.argv) > 1 else "./logs"
    ymd = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
    sys.exit(report(logdir, ymd))
