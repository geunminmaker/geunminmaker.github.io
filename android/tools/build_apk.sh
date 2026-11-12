#!/usr/bin/env bash
# Build the signed APKs (release + dev), copy them to dist/ and verify them.
#
#   Shell : Git Bash on Windows (also runs on Linux / macOS).
#   Usage : tools/build_apk.sh [extra Gradle arguments]
#           tools/build_apk.sh -PtwaVersionName=1.0.1 -PtwaVersionCode=2
#
# Needs
#   - JDK 17 or newer (JAVA_HOME or java on PATH; built and tested with JDK 21)
#   - an Android SDK with platforms/android-36 and build-tools/35.0.0, found through TWA_SDK_DIR,
#     then sdk.dir in local.properties, then ANDROID_HOME / ANDROID_SDK_ROOT
#   - signing values in keystore.properties (or TWA_KEYSTORE_FILE / TWA_KEYSTORE_PASSWORD /
#     TWA_KEY_ALIAS / TWA_KEY_PASSWORD in the environment)
#
# Result
#   dist/jogakjogak.apk       the app            (io.github.geunminmaker.splitimage)
#   dist/jogakjogak-dev.apk   emulator test twin (io.github.geunminmaker.splitimage.dev, http://localhost:8765/)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
cd "$ROOT"

# ---- locate the SDK and its build tools ------------------------------------------------------
SDK_DIR="${TWA_SDK_DIR:-}"
if [ -z "$SDK_DIR" ] && [ -f local.properties ]; then
  # local.properties escapes ':' and '\' (C\:\\Users\\... or C\:/Users/...)
  SDK_DIR="$(sed -n 's/^sdk\.dir=//p' local.properties | tail -n 1 | tr -d '\r' | sed 's/\\:/:/g; s/\\\\/\//g')"
fi
SDK_DIR="${SDK_DIR:-${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}}"
[ -n "$SDK_DIR" ] || { echo "No Android SDK found: set TWA_SDK_DIR or sdk.dir in local.properties" >&2; exit 1; }
[ -d "$SDK_DIR/platforms/android-36" ] || { echo "Missing $SDK_DIR/platforms/android-36" >&2; exit 1; }
BT="$(ls -d "$SDK_DIR"/build-tools/*/ 2>/dev/null | sort -V | tail -n 1)"
BT="${BT%/}"
[ -f "$BT/lib/apksigner.jar" ] || { echo "No build-tools with apksigner below $SDK_DIR/build-tools" >&2; exit 1; }
AAPT2="$BT/aapt2"; [ -f "$AAPT2.exe" ] && AAPT2="$AAPT2.exe"
JAVA="java"; [ -n "${JAVA_HOME:-}" ] && JAVA="${JAVA_HOME%[\\/]}/bin/java"
apksigner() { "$JAVA" -jar "$BT/lib/apksigner.jar" "$@" | tr -d '\r'; }

# ---- build -----------------------------------------------------------------------------------
GRADLEW=./gradlew
if [ -f gradlew.bat ] && command -v cmd.exe >/dev/null 2>&1; then GRADLEW=./gradlew.bat; fi
rm -rf app/build/outputs/apk          # so an APK of an older version name can never be picked up
"$GRADLEW" --no-daemon --console=plain :app:assembleRelease :app:assembleDev "$@"

# ---- copy + verify ---------------------------------------------------------------------------
mkdir -p dist
verify() {  # verify <build type> <dist file name>
  local built
  built="$(ls app/build/outputs/apk/"$1"/*-"$1".apk 2>/dev/null | head -n 1 || true)"
  if [ -z "$built" ]; then
    echo "No signed $1 APK was produced (unsigned build? check keystore.properties)" >&2
    exit 1
  fi
  cp "$built" "dist/$2"
  echo
  echo "== dist/$2  ($(basename "$built"))"
  echo "   size   $(wc -c < "dist/$2" | tr -d ' ') bytes"
  echo "   sha256 $(sha256sum "dist/$2" | cut -d' ' -f1)"
  "$AAPT2" dump badging "dist/$2" | tr -d '\r' \
    | grep -E "^package:|^minSdkVersion|^targetSdkVersion|^application-label:|^application-label-ko:|^application-icon-" \
    | sed 's/^/   /'
  # --min-sdk-version 21 makes apksigner report the v1 (JAR) signature too; with the real
  # minSdk 24 it skips that check and prints "v1 scheme: false".
  apksigner verify --verbose --min-sdk-version 21 --print-certs "dist/$2" \
    | grep -E "^Verifies|^Verified using v[123] |certificate DN|certificate SHA-256" | sed 's/^/   /'
}
verify release jogakjogak.apk
verify dev jogakjogak-dev.apk

# ---- Digital Asset Links fingerprint ---------------------------------------------------------
FP="$(apksigner verify --print-certs dist/jogakjogak.apk | grep -m1 'certificate SHA-256 digest' \
      | awk '{print toupper($NF)}' | sed 's/../&:/g; s/:$//')"
echo
echo "Release certificate SHA-256 (assetlinks.json): $FP"
if grep -q "$FP" assetlinks.json 2>/dev/null; then
  echo "assetlinks.json already lists this fingerprint."
else
  echo "WARNING: assetlinks.json does NOT list this fingerprint - the app would open with a browser bar." >&2
  exit 1
fi
