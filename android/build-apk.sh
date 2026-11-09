#!/usr/bin/env bash
# Build the HAVen Android app and put the APK in android/dist/.
#
#   android/build-apk.sh             # debug-signed APK, installable by sideloading
#
# Needs a JDK (17 or newer) and the Android SDK (ANDROID_HOME, or local.properties with sdk.dir=...).
# The first build downloads the Android Gradle Plugin; later builds work offline.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
if [[ ! -f local.properties && -z "${ANDROID_HOME:-}" && -d "$HOME/Library/Android/sdk" ]]; then
  printf 'sdk.dir=%s\n' "$HOME/Library/Android/sdk" > local.properties
fi
./gradlew --no-daemon -q testDebugUnitTest assembleDebug
version="$(sed -n 's/.*versionName = "\(.*\)".*/\1/p' app/build.gradle.kts)"
mkdir -p dist
cp app/build/outputs/apk/debug/app-debug.apk "dist/HAVen-$version-debug.apk"
echo "dist/HAVen-$version-debug.apk"
