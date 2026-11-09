# HAVen — Devpost submission

> Paste each section into the matching Devpost field. Items marked **TODO** need the team.

---

## Project name
HAVen

## Tagline (Devpost "elevator pitch", ≤ 200 chars)
A forearm sleeve that measures grip effort and wrist vibration together, so exposure to vibrating tools is something workers can see on their phone, not a guess.

---

## About the project

### Inspiration
Hand-arm vibration syndrome (HAVS) comes from years of regular work with vibrating hand tools: grinders, breakers, drills, sanders. It causes numbness, lost grip and white fingers, and the damage does not go away. Yet on most job sites, exposure is still estimated on paper: the tool's rated vibration multiplied by the hours someone thinks they used it.

That number says nothing about the person holding the tool. How hard someone grips changes how much vibration reaches the arm, and nobody writes down the ten-minute bursts that add up over a shift. We wanted to see whether a cheap wearable could measure what the worker's own arm is doing, and turn it into something a worker can glance at.

### What it does
HAVen is a sleeve worn on the forearm with three readings that we deliberately keep separate:

- **Grip effort:** a forearm EMG signal, normalised to the wearer's own two-step calibration (relaxed, then a hard squeeze). It is relative effort, not force.
- **Wrist vibration:** from an accelerometer on the wrist, sampled at 500 Hz, with **tool-use detection** that tells a running tool apart from ordinary arm movement and taps, using band energy, periodicity and steadiness. It tracks tool time and bouts automatically.
- **Exposure:** a daily vibration exposure estimate that only counts time while a tool is detected. It follows the ISO 5349-1 A(8) arithmetic and shows progress towards the EU/UK action and limit values. There is also a separate experimental score that combines vibration with grip effort.

The sleeve streams over Wi-Fi straight to a phone. The **Android and iOS apps do all the processing on the phone** with no laptop or server: a guided setup (one step per screen with a countdown), a live dashboard with an exposure ring for today's shift, alarms at the action level and the daily limit (they keep working in the background), and a daily and weekly history. A NeoPixel on the sleeve mirrors the colour of the phone's ring, so the wearer can see their status without taking out the phone.

For supervisors or demos there is also a laptop mode. A local backend records sessions, replays them and serves a web dashboard with live charts, calibration, history and an overview of recordings. It also includes a **fingertip vibration-perception test**: a small haptic motor buzzes at different levels, and the wearer answers felt / not felt.

Every screen says where its data comes from (**live**, **replay** or **simulated**), and simulated data is striped and labelled the whole time.

### How we built it
**Hardware.** Two ESP32 boards (ESP32-D0WD-V3). The wearable board reads an Upside Down Labs BioAmp EXG Pill (EMG) on its ADC and an MPU-6500 IMU over I²C, with gel electrodes held in place by a tattoo-style sleeve. The station board drives an ERM vibration motor through a TI DRV2605L haptic driver.

**Firmware (C, ESP-IDF v6.1).** EMG is sampled at 20 kHz by hardware using continuous ADC with DMA and averaged down to 1 kHz, with timestamps taken from the conversion count. IMU samples get timestamps from a phase- and frequency-locked estimator of the IMU's own clock. The board sends batched records in a versioned NDJSON telemetry contract. It finds a laptop bridge by UDP discovery, and when none answers it connects to the phone hotspot's gateway. The station firmware uses a separate, validated command protocol with bounded stimulus levels and durations, duplicate and busy rejection, and an abort that stops the motor. Portable C is host-tested with CTest, and a fake device runs the same telemetry pipeline on a simulated clock.

**Signal processing (Python).** The reference processor contains Butterworth filtering, the EMG envelope, relaxed/squeeze calibration, vibration features, tool-use detection with its own still / movement / tool-on calibration, the daily exposure estimate and the experimental score. It also includes a strict contract validator and a deterministic simulator. Everything is tested with pytest.

**Phone apps.** We ported the processor twice: to Swift (SwiftUI, Foundation only, so the tests also run on Linux) and to Java (plain Android framework, no AndroidX). Both ports are tested on **real recordings from the sleeve**, including a grip calibration and a tool calibration with a massage gun, and their outputs match the Python processor's on every compared value. There is also an Expo / React Native app that reuses the web dashboard's adapters for laptop mode.

**Backend and dashboard.** The backend is a single local process: FastAPI, SQLite, WebSocket live delivery, and serial, Wi-Fi, simulated and replay adapters. It handles sessions, recording and replay, provenance tracking and a recorded backup demo. Its acceptance suite checks 16 scenarios, including malformed frames, sequence gaps, disconnects, idempotent commands and CSRF / DNS-rebinding protection. The dashboard and landing page are plain HTML, CSS and JavaScript with no build step and no third-party requests.

### Challenges we ran into
- **Not trusting the labels.** The IMU modules were sold as "MPU-9250/6500". Reading WHO_AM_I showed an MPU-6500, which has a different scale. The BioAmp shipped in its EEG/EOG configuration, not EMG. We read the chips' identity and register values on the hardware and logged each fact with its evidence before writing code that depended on it.
- **Timing.** Polling the ADC in software gave uneven EMG timing. Moving to sampling paced by DMA, and reconstructing the IMU's clock, got us uniform batches with 0 dropped samples over 80,000+ records.
- **Phone-hotspot Wi-Fi.** Normal 250–500 ms pauses on the hotspot made the stream flicker to "unknown" and overflowed the transmit queue. Retuning the stale limits, the queue size and the TCP window brought it to 0 dropped records in our soak test.
- **Telling a tool from an arm.** Swinging your arm produces bigger accelerations than many tools do. We separate them by frequency band, periodicity and steadiness, and add a per-user tool calibration. Taps, knocks and vigorous movement stay "no tool", and a vibrating source is picked up within seconds.
- **Two ports that agree.** Keeping the Swift and Java engines in sync with the Python processor took fixture-based tests on real recordings, down to the calibration thresholds.
- **Being honest under time pressure.** It is tempting to call a number a "dose" or draw a "safe" line on a chart. We built provenance labels into the data contract itself, so simulated data can never be shown as live.

### Accomplishments that we're proud of
- An end-to-end chain that works on real hardware: sleeve → Wi-Fi → phone, with every calculation done on the phone.
- About 41 records/s of EMG + IMU over Wi-Fi with 0 sample drops, and tool detection working live on the board.
- The same signal processing in three languages (Python, Swift, Java), checked against each other on real recordings.
- Haptic-motor pulses with timing measured on the board (a 200 ms command gave 200.001 ms of drive) and an abort that stops the motor on the same 1 ms tick.
- A worker-facing app designed for people who may be new to smartphones: large buttons, plain words, one step per screen.
- A record of what is verified, kept by evidence level (host-tested → compiled for the board → flashed → measured on hardware → tested on a wearer), so we know exactly what we can claim.

### What we learned
- Measure the hardware before trusting a datasheet, the seller's listing or the silkscreen.
- On the embedded side, timing is the hard part. Getting the values right is the easy part.
- A written, versioned data contract let five people work in parallel against a fake device long before the real one worked.
- Being clear about what a number is *not* (not a dose, not force, not a diagnosis) makes a health-adjacent prototype more credible, not less.

### What's next for HAVen
- Validate on real power tools against a reference accelerometer, and test on more wearers.
- Dry electrodes built into the sleeve, so there is no gel.
- A smaller enclosure on a battery for a full shift.
- Study whether grip effort really changes how much vibration reaches the arm. That relationship is the core idea we are prototyping.
- Exposure reports a team can share with a supervisor, with the worker's consent.

### Limitations (important)
HAVen is a hackathon prototype, **not a medical device**. Grip effort is relative to the wearer's calibration, not grip force or %MVC. The vibration is measured by a hobby-grade sensor and is not compliant ISO 5349 dosimetry. The experimental score is in arbitrary units. The fingertip test does not detect nerve damage, and its motor settings are drive levels, not measured amplitudes. Tool detection was tested with stand-ins (a phone motor and a massage gun), not industrial tools. Historical demo data is simulated and labelled as such.

---

## Built with
`esp32` `esp-idf` `c` `python` `fastapi` `sqlite` `websockets` `javascript` `html` `css` `swift` `swiftui` `java` `android` `react-native` `expo` `typescript` `bioamp-exg-pill` `mpu-6500` `drv2605l` `emg` `pytest` `uv` `cmake`

## Try it out (links)
- GitHub: https://github.com/msbutt1/HAVen
- Live demo (simulated data, runs in the browser): **TODO**: run `landing/build-site.sh` and host `site/` (e.g. GitHub Pages), then paste the URL
- Android APK: **TODO**: attach `android/dist/HAVen-0.1.0-debug.apk` to a GitHub release and paste the link

## Video demo
**TODO**: YouTube/Vimeo link (Devpost embeds it). Suggested 2-minute flow: put on the sleeve → guided calibration (relax, squeeze) → tool on, the ring fills and tool time counts → arm waving stays "no tool" → NeoPixel colour → fingertip test → history/overview.

## Image gallery (3:2 works best)
- `landing/assets/dashboard-light.png`, `landing/assets/dashboard-dark.png` (web dashboard; the banner says replay)
- **TODO**: photo of the sleeve being worn, the boards and wiring, phone screenshots (setup, today's ring, alarm, weekly overview), the station motor

## Team
**TODO**: confirm names and Devpost handles
- Collins: hardware and wiring
- Salaar: firmware, signal processing, phone apps
- Abduljabbar: backend, web dashboard, landing page, Expo app
- Ali: frontend
- Fahad: exposure estimate and limits, Android history and overview, station software and integration
