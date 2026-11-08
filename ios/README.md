# VibeGuard for iOS

A native SwiftUI app for the VibeGuard backend: the same five screens as the web dashboard (Live, Summary, Calibrate,
Touch test, History), in the same design (the landing page's livops.uz colour tokens, light and dark), laid out for
people who may be new to phones: large type and buttons, the main action of each screen first, plain labels, with the
technical details still there further down.

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

A fresh install opens on the **connect screen**: it searches the network for laptops that announce VibeGuard with
Bonjour (`_vibeguard._tcp`, sent by `serve` when it listens with `--host 0.0.0.0`), and lists each as one big **Open**
button; **Can't connect?** has the steps and a box to type an address (in the Simulator, `localhost` is the Mac).
After that the app reopens the same laptop by itself; the **Laptop** button (top left) changes it. (Port 8765 belongs
to the wearable's Wi-Fi telemetry; the web API is on 8000.)

Why Bonjour and not the Android app's UDP broadcast: since iOS 14 an app may not send broadcast or multicast packets
without an entitlement Apple grants on request, but it may browse Bonjour services (`NSBonjourServices` in
`Config/Info.plist`) once the user allows local-network access.

The **Summary** tab is the web dashboard's Overview in Swift Charts: KPI tiles with the change against the previous
period (neutral ink), recordings per day, grip effort, highest scores, tool-time share, touch test answers and
thresholds, grip vs vibration, and where the data came from; no chart draws a safe or unsafe line.

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

1. Start the backend so the phone can reach it, with a password on a network you do not control:
   `VG_BASIC_AUTH='demo:a-long-password' uv run vg-backend serve --device wifi --host 0.0.0.0`
2. In Xcode, pick your team under Signing & Capabilities (the bundle id is `dev.vibeguard.ios`; change it if Xcode
   asks) and run on the phone.
3. Open the app and allow **Local Network** when iOS asks. The laptop appears; tap **Open** (and sign in if it asks
   for the password). If nothing is found, **Can't connect?** lets you type its address (for example `192.168.1.20`).
   The full walk-through, with the wearable: `CONNECT_OVER_WIFI.md` at the top of the repo.

Plain http is allowed only on the local network (`NSAllowsLocalNetworking`); anything on the internet needs https.
The password is stored in the Keychain.

## What is where

| File | What it does |
|---|---|
| `VibeGuard/Backend.swift` | Connection, live data, recording, calibration, history, perception-test runner |
| `VibeGuard/Translate.swift` | Backend records to screen values (pure functions, unit-tested) |
| `VibeGuard/LaptopFinder.swift` | Finds laptops announcing `_vibeguard._tcp` (Bonjour, `NWBrowser`) and resolves their address |
| `VibeGuard/Summary.swift` | The Summary tab's numbers (pure functions, unit-tested; the web Overview's rules) |
| `VibeGuard/Models.swift` | Data types, and the stand-in staircase for the perception test |
| `VibeGuard/Theme.swift` | Colours, panels, buttons and labels from the landing page's design system |
| `VibeGuard/Views/` | The screens |
| `VibeGuardTests/` | Unit tests for the translation rules, the staircase, the Summary numbers and finding the laptop |

The perception test drives the backend's **simulated** station controller (no motor); its results are stored and shown
as simulated. The test sequence is a stand-in until Fahad's engine is connected to the backend.
