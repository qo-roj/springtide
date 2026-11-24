#!/bin/bash
# One-time Android build toolchain (openjdk-21 + SDK cmdline-tools
# + platforms;android-34 + build-tools;34.0.0). Idempotent.
set -e
export SUDO_ASKPASS="$HOME/.local/bin/askpass.sh"
echo "[1/4] apt: openjdk-21 + zip/unzip"
sudo -A apt-get update -qq
sudo -A DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openjdk-21-jdk-headless zip unzip wget >/dev/null
echo "[2/4] SDK cmdline-tools"
mkdir -p "$HOME/Android/Sdk/cmdline-tools"
Z="$HOME/.hermes/cache/scratch/commandlinetools-linux-11076708_latest.zip"
if [ ! -s "$Z" ]; then
  wget -4 -q --show-progress -O "$Z" https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
fi
unzip -q -o "$Z" -d "$HOME/Android/Sdk/cmdline-tools"
[ -d "$HOME/Android/Sdk/cmdline-tools/cmdline-tools" ] && mv -T "$HOME/Android/Sdk/cmdline-tools/cmdline-tools" "$HOME/Android/Sdk/cmdline-tools/latest"
export ANDROID_HOME="$HOME/Android/Sdk"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
echo "[3/4] licenses"
yes | sdkmanager --licenses >/dev/null
echo "[4/4] platform + build-tools"
yes | sdkmanager "platforms;android-34" "build-tools;34.0.0" >/dev/null
java -version 2>&1 | head -1
ls "$HOME/Android/Sdk/platforms" "$HOME/Android/Sdk/build-tools"
echo TOOLCHAIN_DONE