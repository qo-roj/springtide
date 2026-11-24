#!/bin/sh
# build.sh — sync the portable core into the sketch folder, then compile.
#
# Source of truth for the KW1281 core is firmware/kw1281/; the Arduino
# toolchain only compiles the sketch folder, so the core is copied in
# (never edited here) before every build.
#
# Usage: firmware/build.sh            (compile only)
#        FQBN=esp32:esp32:esp32doit-devkit-v1 firmware/build.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FQBN=${FQBN:-esp32:esp32:esp32doit-devkit-v1}

cp "$HERE/kw1281/kw1281.h" "$HERE/springtide_node/kw1281.h"
cp "$HERE/kw1281/kw1281.c" "$HERE/springtide_node/kw1281.c"

exec arduino-cli compile --fqbn "$FQBN" "$HERE/springtide_node"
