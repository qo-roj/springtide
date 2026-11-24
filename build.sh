#!/bin/bash
# Build the OBD bridge APK. Requires: openjdk-21, Android SDK build-tools;34.0.0,
# platforms;android-34 (installed by toolchain.sh), zip.
set -e
SDK="$HOME/Android/Sdk"
# build-tools 34.0.0 ships a broken dev d8 (NPE on anonymous inner classes) — use 35.0.0.
BT="$SDK/build-tools/35.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
cd "$(dirname "$0")"
rm -rf build
mkdir -p build/obj build/dex
echo "[aapt2] link manifest"
"$BT/aapt2" link -o build/base.apk --manifest AndroidManifest.xml -I "$PLATFORM"
echo "[javac] compile"
javac -source 8 -target 8 -Xlint:-options -nowarn -cp "$PLATFORM" -d build/obj \
  app/src/com/springtide/bridge/BridgeService.java \
  app/src/com/springtide/bridge/LauncherActivity.java \
  app/src/com/springtide/bridge/BootReceiver.java
echo "[d8] dex"
"$BT/d8" --release --lib "$PLATFORM" --min-api 26 --output build/dex \
  build/obj/com/springtide/bridge/*.class
echo "[package]"
cp build/base.apk build/unsigned.apk
(cd build/dex && zip -q -j ../unsigned.apk classes.dex)
if [ ! -f debug.keystore ]; then
  keytool -genkeypair -keystore debug.keystore -storepass springtide -keypass springtide \
    -alias bridge -dname "CN=OBD Bridge, O=Springtide" -keyalg RSA -keysize 2048 \
    -validity 10000 >/dev/null
fi
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
"$BT/apksigner" sign --ks debug.keystore --ks-pass pass:springtide --key-pass pass:springtide \
  --out obdbridge.apk build/aligned.apk
"$BT/apksigner" verify --print-certs obdbridge.apk | head -2
echo "BUILT: $(pwd)/obdbridge.apk ($(stat -c%s obdbridge.apk) bytes)"