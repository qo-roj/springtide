#!/data/data/com.termux/files/usr/bin/python3
"""dash.py — in-car status screen for the DPF monitor.

Full-screen curses view of ~/dpf/live.json (written by springtide_daemon.py every
loop). Keeps the screen on while it runs and restores the normal timeout
on exit. Run it as `dash` (alias is installed in ~/.bashrc).

Optional by design: voice alerts work without this. It just gives the
mounted-phone glanceable numbers.
"""
import curses
import json
import os
import subprocess
import time
from datetime import datetime

BASE = os.path.expanduser("~/dpf")
LIVE_PATH = os.path.join(BASE, "live.json")
CMD_PATH = os.path.join(BASE, "cmd")           # daemon command inbox
CLEAR_DTC_CMD = os.path.join(CMD_PATH, "clear_dtc")

PHASES = {
    # phase -> (display text, color pair id)
    "idle": ("WARMING UP — building baseline", 3),
    "normal": ("ARMED — no regeneration detected", 2),
    "candidate": ("REGEN CANDIDATE — confirming", 4),
    "regen": ("REGENERATION ACTIVE — KEEP DRIVING", 1),
}


def safe_add(scr, y, x, txt, *attrs):
    """addstr that can never kill the dash: truncates to the screen and
    swallows curses.error (small terminals, bottom-right corner writes)."""
    try:
        h, w = scr.getmaxyx()
        if y >= h or x >= w:
            return
        scr.addstr(y, x, txt[: max(0, w - x - 1)], *attrs)
    except curses.error:
        pass


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def stayon(mode):
    subprocess.run(["su", "-c", f"svc power stayon {mode}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    for pid, fg in ((1, curses.COLOR_RED), (2, curses.COLOR_GREEN),
                     (3, curses.COLOR_YELLOW), (4, curses.COLOR_MAGENTA),
                     (5, curses.COLOR_CYAN)):
        curses.init_pair(pid, fg, -1)

    stayon("true")
    try:
        while True:
            live = load(LIVE_PATH)
            h, w = stdscr.getmaxyx()
            stdscr.erase()

            safe_add(stdscr, 0, 0, " SPRINGTIDE ", curses.A_REVERSE)
            safe_add(stdscr, 0, 15, datetime.now().strftime("%H:%M:%S"), curses.A_DIM)

            if not live or time.time() - live.get("ts", 0) > 30:
                safe_add(stdscr, 2, 0, "daemon silent — no data", curses.color_pair(3))
                safe_add(stdscr, 4, 0, "(daemon down, or restarting)")
            else:
                if not live.get("ecu_up"):
                    safe_add(stdscr, 2, 0, "no ECU — ignition off or dongle absent",
                                  curses.color_pair(3))
                txt, pair = PHASES.get(live.get("phase", "idle"), ("", 5))
                if txt:
                    safe_add(stdscr, 4, 0, txt[: w - 1], curses.color_pair(pair) | curses.A_BOLD)

                c = live.get("coolant")
                b = live.get("baseline")
                safe_add(stdscr, 6, 0, f"coolant   {c if c is not None else '--'} C")
                safe_add(stdscr, 7, 0, f"baseline  {b if b is not None else '--'} C"
                                    f"   ({live.get('n_warm', 0)} warm samples)")
                if c is not None and b is not None:
                    safe_add(stdscr, 8, 0, f"delta     {c - b:+.1f} C")
                safe_add(stdscr, 10, 0, f"speed     {live.get('speed')} km/h"
                                    f"     rpm {live.get('rpm')}")

                dtc = live.get("dtc") or []
                if dtc:
                    pend = ("  [clear queued]" if live.get("dtc_clear_pending")
                            else "  - c clears")
                    safe_add(stdscr, 9, 0, f"faults    {', '.join(dtc)}{pend}",
                                  curses.color_pair(1) | curses.A_BOLD)

                if live.get("phase") == "regen":
                    el = live.get("regen_elapsed_s") or 0
                    km = live.get("regen_km") or 0.0
                    pk = live.get("regen_peak")
                    safe_add(stdscr, 12, 0,
                                  f"regen     {int(el // 60)}:{int(el % 60):02d}"
                                  f"   ~{km:.0f} km   peak {pk if pk is not None else '--'} C",
                                  curses.color_pair(1) | curses.A_BOLD)

                le = live.get("last_event")
                if le:
                    t = datetime.fromtimestamp(live.get("last_event_t", 0))
                    safe_add(stdscr, h - 2, 0,
                                  f"last event: {le} at {t.strftime('%H:%M:%S')}",
                                  curses.A_DIM)

            safe_add(stdscr, h - 1, 0, " c clear faults  |  q / Ctrl-C exits ",
                          curses.A_DIM)
            stdscr.refresh()
            stdscr.timeout(1000)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                break
            if ch in (ord("c"), ord("C")):
                # queue a fault-memory clear for the daemon (driver's
                # decision, never the daemon's own)
                try:
                    os.makedirs(CMD_PATH, exist_ok=True)
                    with open(CLEAR_DTC_CMD, "w") as f:
                        f.write(str(time.time()))
                except OSError as e:
                    try:
                        safe_add(stdscr, 13, 0, f"clear request failed: {e}",
                                 curses.color_pair(1))
                    except Exception:
                        pass
    finally:
        stayon("false")
        subprocess.run(["termux-media-player", "stop"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    curses.wrapper(main)