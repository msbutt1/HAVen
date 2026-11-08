# VibeGuard dashboard (Ali)

Live view, calibration, perception test, and history screens. No build step, no installs, no internet needed.
It uses the same design system as the landing page (livops.uz's colour tokens for light and dark, Onest and
JetBrains Mono self-hosted in `assets/fonts`, rim-lit panels).

## Run it

Double-click `index.html`, or open it in Chrome. It starts on **mock data** (purple striped band = simulated).

To use the real backend, let the backend serve the dashboard (same origin, nothing to configure):

```
cd backend && uv run vg-backend serve            # then open http://127.0.0.1:8000/dashboard/
```

Served over http(s) the dashboard talks to the backend (`js/backend-adapter.js`: live data over `/ws/live`,
commands and history over `/api/...`); opened as a file it uses the mock feed. `?source=mock|backend` overrides,
`&base=http://host:port` points at a backend on another origin (it must allow that origin).
The perception-test screens run on the backend's station API (`js/backend-station.js`): the backend's controller is
simulated (no motor), so every test is stored and shown as simulated, and the test sequence is a stand-in for Fahad's
engine. The 12-week charts on the History screen are labelled sample data, not your recordings.

With the Wi-Fi wearable (`serve --device wifi`) the Live screen also shows **tool use** (estimated from vibration, with a
**New shift** button) and an **Events** list in plain words, and Calibrate follows Salaar's two-step flow
(Start relaxed, then Start squeeze). Grip effort is relative to the wearer's calibration, not grip force.

Publishing it on livops.uz through a tunnel: see `backend/DEPLOY_LIVOPS.md`.

Each screen has its own link, so a refresh stays put: `#live`, `#calibrate`, `#station`, `#history`.

## Mock controls (bar at the bottom, mock mode only)

| Control | What it tests |
|---|---|
| Hold to squeeze, or hold **S** | Muscle activation rising |
| Tool running | Vibration on, plus the "vibration leaking into EMG" warning |
| Drop connection | Data stops silently: readings hide after 1 s, then "disconnected" after 3 s |
| Inject sleeve error | Red error banner; disconnect and reconnect to clear |

In the perception test, press **F** for "felt it" and **N** for "didn't feel it".

## Files

| File | Owner of the logic | Notes |
|---|---|---|
| `js/contract.js` | Team | **Placeholder message format.** Replace with the agreed contract v1. Example payloads to share. |
| `js/mock-adapter.js` | Ali | Fake sleeve + station. The staircase and score formula are stand-ins for Fahad's and Salaar's real logic. |
| `js/ws-adapter.js` | Ali | Real backend connection. Untested until the backend exists. |
| `js/charts.js` | Ali | Canvas charts, no libraries. |
| `js/app.js` | Ali | All screens. Only talks to the adapter, never to a specific backend. |

## What to send teammates

- **Abduljabbar:** `contract.js` `EXAMPLES`. The backend needs to push those message types over a WebSocket and reply to commands with `{type: "response", command_id, status}`.
- **Fahad:** the station messages (`station_started`, `station_trial`, `station_trial_result`, `station_complete`, `station_aborted`) and the commands `start_station_test`, `abort_station_test`, `submit_response`.

## Acceptance checks covered (Ali_05)

- Disconnects hide stale readings; it never shows old numbers as current.
- Units, calibration state, and the vibration-artifact warning stay visible.
- Station controls follow the engine state; abort is always available while running.
- Simulated data and compressed time are labeled everywhere they appear.
- Works after refresh; dark and light mode both readable.
