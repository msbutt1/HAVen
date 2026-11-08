# VibeGuard for iOS

A native SwiftUI app for the VibeGuard backend: the same four screens as the web dashboard (Live, Calibrate,
perception Test, History), in the same design (the landing page's livops.uz colour tokens, light and dark).

**It is a client of the backend, not of the sleeve.** The sleeve plugs into the computer running the backend over
USB, and an iPhone cannot read that port. The app connects to the backend over the network: live data on the
WebSocket `/ws/live`, commands and history on `/api/...`, translated by the same rules as the web dashboard
(`Translate.swift` mirrors `dashboard/js/backend-adapter.js`). Nothing is invented: a value the backend does not
provide stays empty, every screen says whether data is live, a replay or simulated, and the exposure score is
labelled experimental.

## Run it in the Simulator

```bash
cd backend && uv run vg-backend serve --backup backup/demo-session --loop   # the recorded demo, labelled replay
# or, with the Wi-Fi wearable (or `uv run vg-backend fake-wifi` to simulate one):
cd backend && uv run vg-backend serve --device wifi
open ios/VibeGuard.xcodeproj                                                # then Run (iPhone simulator)
```

The default backend address is `http://localhost:8000`, which is the Mac when running in the Simulator. (Port 8765
belongs to the wearable's Wi-Fi telemetry; the web API is on 8000.)

The Live screen shows grip effort (% of the wearer's calibrated maximum squeeze, not force), wrist vibration, the
experimental score, **tool use** estimated from vibration (with **New shift**) and the latest **events** in plain words.
Calibrate follows the guide's two steps: Start relaxed, then Start squeeze; a failure says why and goes back to step 1.

From the command line:

```bash
cd ios
xcodebuild -project VibeGuard.xcodeproj -scheme VibeGuard -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
xcodebuild -project VibeGuard.xcodeproj -scheme VibeGuard -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```

## Run it on an iPhone

1. Start the backend so the phone can reach it, with a password (the API has no accounts):
   `VG_BASIC_AUTH='demo:a-long-password' uv run vg-backend serve --host 0.0.0.0 --backup backup/demo-session --loop`
2. In Xcode, pick your team under Signing & Capabilities (the bundle id is `dev.vibeguard.ios`; change it if Xcode
   asks) and run on the phone.
3. In the app: Settings, set the address to the Mac's address on the same Wi-Fi (for example `http://192.168.1.20:8000`)
   and the username and password. iOS asks for local-network permission the first time.

Plain http is allowed only on the local network (`NSAllowsLocalNetworking`); anything on the internet needs https.
The password is stored in the Keychain.

## What is where

| File | What it does |
|---|---|
| `VibeGuard/Backend.swift` | Connection, live data, recording, calibration, history, perception-test runner |
| `VibeGuard/Translate.swift` | Backend records to screen values (pure functions, unit-tested) |
| `VibeGuard/Models.swift` | Data types, and the stand-in staircase for the perception test |
| `VibeGuard/Theme.swift` | Colours, panels, buttons and labels from the landing page's design system |
| `VibeGuard/Views/` | The screens |
| `VibeGuardTests/` | Unit tests for the translation rules and the staircase |

The perception test drives the backend's **simulated** station controller (no motor); its results are stored and shown
as simulated. The test sequence is a stand-in until Fahad's engine is connected to the backend.
