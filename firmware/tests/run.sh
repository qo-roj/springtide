#!/bin/sh
# run.sh — host tests for the portable KW1281 core (gcc, no hardware).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
gcc -Wall -Wextra -O2 -o "$HERE/test_kw1281_core" \
    "$HERE/test_kw1281_core.c" "$HERE/fake_ecu.c" "$HERE/../kw1281/kw1281.c" \
    -I"$HERE/../kw1281" -I"$HERE"
"$HERE/test_kw1281_core"
