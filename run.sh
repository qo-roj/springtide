#!/bin/bash
# Springtide daemon supervision loop (Termux has no systemd).
cd "$HOME/dpf" || exit 1
termux-wake-lock 2>/dev/null
mkdir -p logs
while true; do
  python3 springtide_daemon.py >> logs/daemon.log 2>&1
  echo "$(date '+%F %T') daemon exited rc=$? — restarting in 10s" >> logs/daemon.log
  sleep 10
done