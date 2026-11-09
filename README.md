# HAVen

**A forearm sleeve that measures how hard a worker grips and how much a power tool shakes their hand, and tells them when to stop.**

HAVen is a prototype for hand-arm vibration (HAV) exposure. It pairs a wearable sleeve (an ESP32 with a forearm EMG sensor and a wrist accelerometer) with a phone app that does all the processing, and a laptop dashboard for demos and review. It was built for natHacks 2026.

> **Status: hackathon prototype.** HAVen is not a medical device and does not give a diagnosis. Its exposure score is experimental, its vibration numbers are an unweighted wrist estimate, and the demo data is simulated unless labelled otherwise. See [Limitations](#limitations).

---

## Contents

- [Links and supporting materials](#links-and-supporting-materials)
- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Testing](#testing)
- [Limitations](#limitations)
- [Team](#team)

---

## Links and supporting materials

| Material | Link |
|---|---|
| Source code | [github.com/msbutt1/HAVen](https://github.com/msbutt1/HAVen) |
| Pitch deck (PowerPoint) | [presentation/HAVen_Pitch_Deck.pptx](https://github.com/msbutt1/HAVen/blob/main/presentation/HAVen_Pitch_Deck.pptx) |
| Pitch deck source, speaker notes and build | [presentation/](https://github.com/msbutt1/HAVen/tree/main/presentation) |
| Devpost submission | *to be added* |
| Live demo (simulated data, runs in the browser) | *to be added: the landing page is in [`landing/`](landing/README.md)* |
| Demo video | *to be added* |
| Android app (APK) | *to be added as a release asset, built with [`android/build-apk.sh`](android/README.md)* |

The pitch deck links point at `main`, so they work once the pull request that adds them is merged.

---

## What it does

Hand-arm vibration syndrome comes from years of using vibrating tools. It causes permanent nerve, blood-vessel and muscle damage, and there is no cure. HAVen tries to make exposure visible while a worker is still on the job:

- **Grip effort** from a forearm EMG sensor, shown relative to the wearer's own relaxed and squeeze calibration.
- **Wrist vibration** from an accelerometer, unweighted and measured within the sensor's limits.
- **Real tool time.** Exposure only counts while the tool is shaking *and* the worker is gripping it. A tool-detection step separates a running tool from normal arm movement.
- **Daily exposure against the EU and UK action and limit values**, using the ISO 5349-1 A(8) formula. The app shows points (100 is the action level, 400 is the daily limit), time left before each level, and an alarm at each level.
- **Long-term view:** the day, week, month and year, and an ISO 5349-1 Annex C *group* estimate of years until 10% of similar workers develop white finger. This is a group figure, not a prediction for an individual.

---

## How it works

```
        Wi-Fi                          phone (Android or iPhone)
 sleeve ─────────►  engine: filtering, tool detection,  ─► screens, alarms,
 ESP32 + EMG +      ISO 5349 dose, calibration                 TV meters
 accelerometer                                               (port 8080)
                                  │
                                  └── laptop (optional):  vg-backend
                                      (Python, SQLite, WebSocket)  ─► dashboard
```

- **Sleeve ([`firmware/`](firmware/README.md)):** ESP32 firmware on ESP-IDF. Samples EMG at 1 kHz and the IMU at 500 Hz, and streams a versioned telemetry format over Wi-Fi.
- **Phone ([`android/`](android/README.md), [`ios/`](ios/README.md)):** the phone is the sleeve's receiver and does all the processing live. The Android app is written in Java and the iOS app in SwiftUI. The same signal engine runs in both, and each is checked against real sleeve recordings. The Android app also serves a live TV view on port 8080.
- **Laptop (optional, [`backend/`](backend/README.md), [`dashboard/`](dashboard/README.md)):** a local Python service (FastAPI, SQLite and a WebSocket feed) that stores sessions and serves the web dashboard, including a TV view.
- **Shared processing ([`signals/`](signals/README.md)):** the Python reference implementation of the signal processing, the exposure rules and the telemetry validator. The phone engines are checked against it.

---

## Repository layout

| Path | What it is |
|---|---|
| [`firmware/`](firmware/README.md) | Sleeve firmware (ESP32, ESP-IDF): wearable and station code, shared C, host tests |
| [`signals/`](signals/README.md) | Python reference processing, exposure rules, telemetry contract and simulator |
| [`backend/`](backend/README.md) | Laptop service: sessions, SQLite, WebSocket, dashboard hosting |
| [`dashboard/`](dashboard/README.md) | Web dashboard and TV meters (plain HTML, CSS and JavaScript, no build step) |
| [`android/`](android/README.md) | Android app (Java): the standalone sleeve receiver and processor |
| [`ios/`](ios/README.md) | iOS app (SwiftUI) with the same engine |
| [`mobile/`](mobile/README.md) | Expo app for a phone to show the laptop's data (laptop mode) |
| [`landing/`](landing/README.md) | Static landing page, with a demo that runs in the browser |
| [`presentation/`](presentation/) | Pitch deck: build script, speaker notes, assets and the built `.pptx` |
| [`project-plan/`](project-plan/) | Team plans and the project brief |
| [`DEVPOST.md`](DEVPOST.md) | Devpost submission text |

---

## Getting started

Each part has its own README with the full details. These are the short routes.

### 1. Run the laptop dashboard (simulated data, no hardware)

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```bash
cd backend
uv sync
uv run vg-backend serve
```

Open <http://127.0.0.1:8000/dashboard/>. The wearable is simulated, and every record is labelled as simulated.

### 2. Run the TV meters

With the backend running (or the phone's app running), open `/dashboard/tv.html`. Press **F** for full screen.

- Backend: `http://<laptop>:8000/dashboard/tv.html`
- Phone: `http://<phone>:8080/`, shown in the Android app under Developer options

### 3. Build and install the Android app

You need a JDK 17 or newer and the Android SDK (set `ANDROID_HOME` or `android/local.properties`).

```bash
cd android
./build-apk.sh          # writes dist/HAVen-<version>-debug.apk
```

Install it with `adb install -r dist/HAVen-<version>-debug.apk`, or copy the file to the phone and open it. The APK is debug-signed, so it installs by sideloading and is not for the Play Store.

To use the real sleeve, turn on the phone's hotspot with the name and password flashed into the sleeve (see [`firmware/`](firmware/README.md)), keep the app open, and switch the sleeve on. It connects by itself.

### 4. Build the sleeve firmware

Install ESP-IDF v6.1 as described in [`firmware/VERSIONS.md`](firmware/VERSIONS.md), then:

```bash
cd firmware
tools/build.sh wearable esp32_d0wd_v3_devkit30
```

Host tests run without ESP-IDF, see [Testing](#testing).

### 5. Build the pitch deck

```bash
cd presentation
npm install
node build.js          # writes HAVen_Pitch_Deck.pptx
```

---

## Testing

| What | Command | Needs |
|---|---|---|
| Signal processing and exposure rules | `cd signals && uv run pytest -q` | uv |
| Backend (simulated, offline) | `cd backend && uv run pytest -q` | uv, about 2 minutes |
| Firmware host tests | `cd firmware && cmake -S tests/host -B build/host -G Ninja && cmake --build build/host && ctest --test-dir build/host` | CMake, Ninja |
| Android unit tests (includes real-recording checks) | `cd android && ./gradlew testDebugUnitTest` | JDK 17+, Android SDK |
| iOS engine tests | `cd ios && swift test` | Swift 5.9+ |
| Mobile app | `cd mobile && npm test` | Node |

On the current branch, the backend suite has a small number of serial and timing tests that depend on the host system. They fail the same way on the commit before the rename, so they are known and not caused by recent changes.

---

## Limitations

- **Prototype hardware.** The sleeve uses development boards and gel electrodes. The production design in the deck is a concept render.
- **Vibration is an estimate.** The wrist accelerometer is unweighted and not a certified ISO 5349 measurement. The app reports an estimate, not an official exposure value.
- **Grip effort is relative.** It is measured against the wearer's own calibration. It is not force or % of maximum voluntary contraction.
- **The exposure score is experimental,** in arbitrary units. It is not a dose and has no safe or unsafe cut-off.
- **Years to risk is a group estimate** from ISO 5349-1 Annex C. It is not a prediction for one person.
- **Limited testing.** Tool detection has been tested with a phone motor and a massage gun, not industrial tools.
- **Demo data is simulated** unless a screen says otherwise.

---

## Team

| Name | Role |
|---|---|
| Fahad | Exposure engine, frontend, dashboard, presentation coordination |
| Salaar | Firmware, signal processing, Android and iOS apps |
| Abduljabbar | Backend, app integration |
| Ali | CAD and parts |
| Collins | Hardware and sleeve |

Built for natHacks 2026, University of Alberta.

---

## Licence

No licence has been chosen yet. Until one is added, all rights are reserved by the team.
