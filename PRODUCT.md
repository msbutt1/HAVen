# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

The worker app is a single HTML page (`android/app/src/main/assets/standalone/index.html`) rendered in an Android WebView, with its own branded look rather than native Material styling. It must stay as light and responsive as it is: CSS and inline SVG only, no frameworks, no remote assets, 60 fps rings.

## Users

Tradespeople who use vibrating power tools (grinders, drills, breakers, sanders) on site. They wear the HAVen sleeve during a shift and glance at the phone between tasks, often with gloves, dirty hands or in bright daylight. Many are new to smartphones. For the natHacks 2026 demo the app is shown to judges exactly as a worker would see it; there is no separate judge mode.

## Product Purpose

Show a worker, in plain words and one glance, how much hand-arm vibration they have taken today and how close they are to the action level and the daily limit, so they can take breaks, switch tools or stop before damage builds up. Success: a worker understands their day from the Today ring without reading, and an alarm reaches them at the action level and the daily limit.

## Positioning

HAVen measures what reaches the worker's own wrist and only counts time while a tool is actually running (tool detection separates tools from normal arm movement), instead of estimating exposure from a tool's rated vibration and guessed hours. Grip effort from forearm EMG is calibrated to the wearer.

## Operating Context

- Sleeve (ESP32, BioAmp EMG, MPU-6500 accelerometer) streams over Wi-Fi to the phone's hotspot; the phone does all processing.
- Setup: a full-screen, one-step-per-screen grip calibration (relax 3 s, squeeze 3 s) each time the sleeve is put on; tool calibration lives in Developer options.
- Alarms sound at the action level (100 points) and the daily limit (400 points, or a lower personal limit), also in the background.
- A TV view of live grip and vibration meters is served by the phone for demos.

## Capabilities and Constraints

- Screens to keep: Today (status pill, Vibration today ring with Low / action level / over limit, tool card, setup card), Overview (today / week / month / year, points or tool time, stats, rules, day by day, How it works), Settings, Developer options, full-screen Setup wizard, full-screen alarm, practice-mode and demo-speed banners.
- Terminology: points (HSE points system), action level (100), daily limit (400), A(8), tool time, practice mode (made-up data), demo speed.
- Colour meaning is fixed: low = green, action level = amber, over the limit = red; simulated or sample data is always visibly labelled.
- Large text and large touch targets stay (users new to smartphones).

## Brand Commitments

- Name: HAVen. Copy may be tightened lightly but must not drift in meaning. The logo may be improved.
- Voice: plain, calm, short sentences a non-expert understands; no jargon on worker screens (technical detail goes to Developer options and How it works).

## Evidence on Hand

- Rules referenced: EU Directive 2002/44/EC, UK Control of Vibration at Work Regulations 2005 and HSE points, ISO 5349-1, ISO/TS 22270.
- No users, testimonials, clinical validation or official certification exist; none may be implied.

## Product Principles

1. One glance tells the day: the ring and one word carry the meaning before any number.
2. Honest by design: an estimate from a wrist sensor, not a medical device or an official measurement; made-up data is always labelled.
3. Calm until it matters: quiet when under the action level, unmistakable at the action level and the limit.
4. Built for gloved hands and first-time phone users: big targets, few choices per screen, nothing moves on by itself in setup.

## Accessibility & Inclusion

Large type (17 px base and up), touch targets of 52 px or more, colour never the only signal (every level has a word), readable in bright sunlight, works in light and dark mode, respects reduced motion.
