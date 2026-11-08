# VibeGuard for Android

An Android app that opens the VibeGuard dashboard from the laptop running the backend, over Wi-Fi. The phone shows
the same screens as the web dashboard (Live, Overview, Calibrate, Perception test, History); the laptop still talks
to the wearable and stores everything.

```
wearable --Wi-Fi--> laptop (vg-backend) <--Wi-Fi-- phone (this app)
```

## Build the APK

```
android/build-apk.sh          # -> android/dist/VibeGuard-0.1.0-debug.apk
```

Needs a JDK 17+ and the Android SDK (found through `ANDROID_HOME` or `android/local.properties`). The first build
downloads the Android Gradle Plugin 9.1.0; Gradle 9.3.1 comes through the wrapper. Plain Android framework, no
AndroidX or Kotlin, so nothing else is downloaded.

The APK is **debug-signed**: fine for installing on team phones by sideloading, not for the Play Store.

## Install it

- USB: `adb install -r android/dist/VibeGuard-0.1.0-debug.apk`
- Or copy the APK to the phone (AirDrop to a Mac first, Google Drive, a USB cable) and open it; Android asks to allow
  installing from that app once.

## Connect over Wi-Fi

1. On the laptop, start the backend so phones on the Wi-Fi can reach it:

   ```
   cd backend && uv run vg-backend serve --host 0.0.0.0
   ```

   With the wearable: `serve --device wifi --host 0.0.0.0`. On a network you do not control, add a password:
   `VG_BASIC_AUTH=team:something-long uv run vg-backend serve --host 0.0.0.0` (the app asks for it).
2. Phone and laptop on the **same Wi-Fi**. macOS may ask whether Python may accept incoming connections: allow it.
3. In the app, tap **Find on this Wi-Fi**, then the laptop in the list. Or type its address (macOS: System Settings ›
   Wi-Fi › Details), e.g. `192.168.1.23` (port 8000 is assumed).

How Find works: the app broadcasts `VGFIND 1` to UDP 8767; the backend (`backend/src/vibeguard_backend/appdiscovery.py`)
answers `VGAPI 1 <http port> <password 0|1> <name>`, only to private addresses and only when it listens beyond the
laptop. The app then checks `/api/health` and opens `http://<laptop>:<port>/dashboard/`.

Some networks keep devices apart (eduroam and most guest or campus Wi-Fi). There, Find gets no answer and the address
does not open: use a phone hotspot (laptop joins the phone's hotspot) or a home router.

## The demo

**Open the demo (simulated)** runs the dashboard bundled in the APK on its simulated feed, with no laptop. It is the
repo's `dashboard/` folder as it was when the APK was built, labelled simulated everywhere.

## What it is, honestly

- A native connect screen plus the dashboard in a WebView, not a native rewrite: one dashboard to maintain, and the
  phone always shows the backend's own copy, so the app never drifts from the backend's version.
- Plain http on the local network (a LAN address has no certificate); cleartext is allowed for that reason
  (`res/xml/network_security_config.xml`). Use `VG_BASIC_AUTH` off your own network.
- Tested on the Android emulator (API 35) against a local backend, including Find (the emulator reaches the Mac as
  10.0.2.2). Not yet tested on a physical phone or a real Wi-Fi network.

| File | What |
|---|---|
| `app/src/main/java/.../MainActivity.java` | Connect screen: Find, typed address, last used, demo |
| `app/src/main/java/.../Discovery.java` | The UDP query and replies |
| `app/src/main/java/.../Address.java` | Typed address to base URL; the `/api/health` check |
| `app/src/main/java/.../DashboardActivity.java` | The dashboard WebView: stays on one origin, asks for the password, error screen |
| `app/src/test/java/.../ConnectTest.java` | Unit tests (`./gradlew testDebugUnitTest`) |
