# VibeGuard for iOS

A native SwiftUI app that reads the VibeGuard sleeve directly and does every calculation on the phone: grip
effort from the EMG, vibration, tool use vs normal arm use, tool time, and the experimental exposure score.
No laptop and no server are needed.

Two pages:
- **Dashboard:** the exposure bar for this shift, tool in use or not with tool time and warnings, grip effort,
  vibration, and calibration status. **Calibrate** opens the grip (relax, squeeze) and tool (still, normal
  movement, tool on) calibrations.
- **Overview:** what the exposure score is, how it is calculated (with your live numbers), and what it means.

Settings (gear): data source (sleeve or the in-app simulated sleeve), accelerometer range, the exposure bar's
shift reference, connection details and recent events.

## How the phone talks to the sleeve

1. Turn on **Personal Hotspot** on the iPhone, with the name and password flashed into the sleeve
   (`firmware/wearable/main/wifi_secrets.h`).
2. Open the app (it starts listening on TCP port **8765**) and keep it in the foreground: iOS suspends
   background apps, so the app keeps the screen awake while it runs.
3. Power the sleeve. It joins the hotspot, broadcasts its discovery request, and when no laptop bridge answers
   it connects to the hotspot gateway, which is the phone. Data appears within a few seconds.

If a laptop running `vg-bridge` is on the same hotspot, the sleeve connects to the laptop instead (discovery is
tried first). Stop the bridge to use the app. The gateway fallback needs firmware with
`firmware/target/vg_wifi_link.c` from 2026-11-08 or later.

iOS asks for local-network permission the first time.

## The calculations

`VibeGuard/Engine/` is a Swift port of the Python processor (`signals/src/vibeguard_signals/processing`):
the same Butterworth filters, EMG envelope, relaxed/squeeze calibration, vibration features, tool detection
(periodicity, steadiness, tool calibration) and score formula. It uses Foundation only, so it builds and is
tested on Linux too:

```bash
cd ios && swift test -c release        # needs a Swift 5.9+ toolchain
```

The tests feed **real recordings from the sleeve** (board W, 2026-11-08, `EngineTests/Fixtures`) through the
Swift engine and compare with the Python processor's outputs on the same data, including a grip calibration
and a tool calibration with the massage gun: envelope, grip %, vibration, tool band, arm band, periodicity,
dominant frequency, tool state and the calibrated tool thresholds match exactly. Regenerate the fixtures with
`EngineTests/make_fixtures.py`.

One deliberate difference: the score's interval bookkeeping is simpler than the Python engine's (each vibration
update covers the time since the last one, at most 0.25 s, and counts only with fresh, calibrated grip). On the
recordings the totals agree to within 0.000001 au.

## Build and run

```bash
open ios/VibeGuard.xcodeproj            # then Run, on an iPhone (the Simulator cannot reach the sleeve)
```

In the Simulator, choose **Simulated sleeve (demo)** in Settings: it produces labelled simulated data with
controls for squeezing, arm movement and a running tool.

On a real iPhone: pick your team under Signing & Capabilities (bundle id `dev.vibeguard.ios`; change it if Xcode
asks).

## What is where

| File | What it does |
|---|---|
| `VibeGuard/SleeveLink.swift` | TCP listener on port 8765 (Network framework), NDJSON lines from the sleeve |
| `VibeGuard/Engine/` | The on-phone processor (filters, records, EMG, vibration, tool use, score) |
| `VibeGuard/AppModel.swift` | Connects the link, the engine, the calibrations and the screens |
| `VibeGuard/DemoSleeve.swift` | The in-app simulated sleeve |
| `VibeGuard/Store.swift` | Calibrations kept between launches |
| `VibeGuard/Views/` | Dashboard, Overview, Calibrate (sheet), Settings |
| `EngineTests/` | Linux/SwiftPM tests of the engine against the Python processor |
| `VibeGuardTests/` | Xcode unit tests (translation rules, staircase) |

Limits, shown in the app: grip effort is relative to your own calibration (not force, not %MVC); the exposure
score is experimental, in arbitrary units (not a dose, not A(8)); the bar's reference and the tool warnings are
team settings, not legal limits; the vibration is unweighted and measured on the forearm.
