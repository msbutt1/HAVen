# Dashboard test log (Ali)

Checks from `project-plan/Ali/Ali_05_testing_and_acceptance.md`.
Test link: https://raw.githack.com/msbutt1/VibeGaurd/main/dashboard/index.html

**Build/version:** the short commit code shown next to the latest commit on GitHub (e.g. `5637c0e`).
**Owner:** Ali for every row unless noted.
**Result:** write PASS or FAIL, and what you actually saw if it differs.

## Run now (mock data)

| # | Scenario | Steps | Expected | Observed / result | Build | Limitation |
|---|---|---|---|---|---|---|
| 1 | Disconnect hides stale readings | Live tab → click **Drop connection** | Within ~1 s numbers turn to "—" and a red "No new reading" message appears; after ~3 s both devices show Disconnected | | | |
| 2 | Reconnect recovers | After test 1, click **Connect** | Numbers return, devices show Ready | | | |
| 3 | Calibration state visible | Live tab before calibrating | Activation shows "—" and "Not calibrated" | | | |
| 4 | Calibration flow | Calibrate tab → **Start calibration**, wait 6 s | Steps 1→2→3 highlight, countdown runs, rest/max values appear, "Calibration saved" | | | |
| 5 | Activation responds | Live tab → hold **S** | Activation rises toward 100, then falls on release | | | |
| 6 | Units and limitations visible | Look at all three Live cards | % / m/s² / points per min shown; vibration says "estimate"; score marked Experimental | | | |
| 7 | Signal-quality warning | Tick **Tool running** | Vibration jumps to ~4; yellow warning about vibration in the muscle signal appears | | | |
| 8 | Session saves | **Start recording**, wait 5 s, **Stop recording** → History tab | Row appears with duration, score, peak vibration, Simulated tag | | | |
| 9 | Disconnect during recording | Start recording → **Drop connection** | Recording saved as "ended early" in History, not lost silently | | | |
| 10 | Station full flow | Perception test → **Start test**, answer with F / N | Trial counter, response bar, buttons enable only when waiting; result + trial log at end; saved to History | | | |
| 11 | Missing response | During a test, don't answer one trial | Row shows "No response" in red; result counts 1 missing | | | |
| 12 | Station abort | Start test → **Abort test** | Returns to start screen with "Test stopped… Nothing was saved" | | | |
| 13 | Error state | **Inject sleeve error** | Red error banner; Record and Calibrate buttons disabled; Disconnect + Connect clears it | | | |
| 14 | Simulated labelling | Check top band and History | Purple striped band says simulated; 12-week chart tagged Simulated and "time is compressed" | | | |
| 15 | Works after refresh | On any tab press **F5** | Same tab reopens and data resumes (mock history resets; real history needs backend) | | | |
| 16 | Legible on judging laptop | Open on the laptop used for judging, full screen, step back 1–2 m | Big numbers and tab names readable | | | |

## Later (needs real sleeve / backend)

| # | Scenario | Needs | Expected | Observed / result | Build | Limitation |
|---|---|---|---|---|---|---|
| 17 | Real data reaches dashboard | Backend + sleeve | Band turns green "Live data", numbers move with real squeeze | | | |
| 18 | Real vibration visible | Sleeve IMU | Vibration rises against a running tool or phone on vibrate | | | |
| 19 | Unplug USB mid-session | Sleeve | Same as tests 1 and 9 | | | |
| 20 | Station drives real motor | Fahad's station | Felt / not felt works with real stimulus | | | |
| 21 | Replay labelled | Backend replay | Blue "Recorded replay" band, never shown as live | | | |
| 22 | History survives refresh | Backend storage | Saved sessions still listed after F5 | | | |
