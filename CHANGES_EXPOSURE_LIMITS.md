# Exposure formula, daily limit and Limits page: change report

Branch `fahad/exposure-limits` (Fahad). Base: `android-app` at `4f73e5e`, plus Salaar's latest
`firmware-esp32` (`0da519b`) merged in so the exposure engine builds on his tool-calibration change.
Everything is additive: nothing was removed, `experimental_score` and all station code still work.

## 1. Summary

- The old experimental score `V² × (1 + a)` (arbitrary units, grip as a multiplier) is no longer the
  headline. The new headline is a **daily vibration exposure estimate**: ISO 5349-1 **A(8)** and
  **HSE points**, compared with the EU/UK **action value (2.5 m/s², 100 points)** and **limit value
  (5.0 m/s², 400 points)**.
- **Grip gates contact time** instead of multiplying the number: a second counts only while the tool
  detector says "tool" and the wearer is not known to be relaxed. Grip during contact is reported next to
  the dose (mean, 10th/50th/90th percentiles), as ISO 5349-1 §4.3 asks for.
- Live tab: new **Daily vibration exposure** card (points bar with 100/400 markers, A(8), points per hour,
  time to the action value and to the limit at the current intensity, gripped vs tool-on contact).
- New **Limits** tab: what the limits are, why, how we calculate, what grip adds, long-term risk, where we
  measure (wrist = monitoring, not measurement), limitations, sources.
- **Perception test tab hidden** (and the station chip and two station charts on Overview). No station code
  was deleted.
- Backend: `exposure` records are published like `experimental_score`, each recording stores an exposure
  summary, and there are three new endpoints (limits, daily totals, personal limit).

## 2. For Abduljabbar (backend, dashboard, Android, iOS)

### Files you own that I touched

| File | Change |
|---|---|
| `backend/src/vibeguard_backend/exposure.py` | **New** (mine): session summary, daily totals, `PersonalLimit` |
| `backend/src/vibeguard_backend/runtime.py` | `latest["exposure"]`; session keeps `start_exposure` / `first_exposure`; on stop `counters["exposure"] = session_exposure_summary(...)`; stale masking treats `exposure` like `experimental_score`; new `DeviceRuntime.set_personal_exposure_limit()` |
| `backend/src/vibeguard_backend/service.py` | `Backend.exposure_limit` (lazy `PersonalLimit`), `set_personal_exposure_limit()`, `exposure_daily()`; `add_runtime` applies a lowered limit to a new processor before its thread starts |
| `backend/src/vibeguard_backend/app.py` | 4 routes under `/api/exposure/...` (below) |
| `backend/src/vibeguard_backend/history.py` | simulated history sessions also get `counters["exposure"]` (so the daily view has sample data) |
| `backend/tests/test_exposure_api.py` | **New**: 7 tests |
| `backend/tests/test_gaps_and_disconnects.py` | one assertion now expects `"exposure": None` in `latest` |
| `dashboard/js/exposure.js` | **New** (mine): renders the card, points maths (`VG.ExposureMath`), personal limit on the Limits page |
| `dashboard/index.html` | exposure card at the top of Live; Limits tab + `#view-limits`; station tab, station chip and 2 Overview station charts get `hidden`; old score card heading → "Legacy score"; Overview footnote updated; `<script src="js/exposure.js">` after `app.js` |
| `dashboard/js/app.js` | `VIEWS` is now `live, overview, calibrate, limits, history` (station removed from the list, so the hidden tab is never shown); `VG.adapter = adapter` (one line, so exposure.js can subscribe) |
| `dashboard/js/backend-adapter.js` | `derived` `exposure` → `emit('exposure', p)`; snapshot `latest.exposure` → `emit('exposure', ...)` |
| `dashboard/js/mock-adapter.js` | simulated exposure with the same maths, emitted every 0.5 s; starts at 62 points of simulated earlier work; "new shift" resets it |
| `dashboard/css/style.css` | appended block: `.exp-*` card and `.lim-*` page styles (no existing rule changed) |

### New WebSocket record: `derived` → `type: "exposure"`

Sent next to every `experimental_score` (one per EMG/IMU feature window, so about 30/s; the dashboard
redraws at most 4/s). Example (SIMULATED, `tool_vs_arm`, trimmed):

```json
{
  "schema": "vibeguard.features", "version": 1, "type": "exposure",
  "device_id": "sim-wearable-01", "boot_id": "sim-example-b0",
  "provenance": {"source": "simulated", "recording_id": "sim-example"},
  "t_us": 54954000, "source_seq": 2252, "trigger": "emg",
  "label": "daily_vibration_exposure_estimate",
  "label_note": "ESTIMATE of ISO 5349-1 A(8) from a wrist-worn IMU (monitoring, not an ISO 5349-2 measurement); ...",
  "status": "accumulating", "reasons": [],
  "gate": "gripped",
  "level": "below_eav",
  "daily":   {"a8_ms2": 0.179158, "points": 0.513563, "percent_of_limit": 0.128391, "contact_s": 26.300007, "energy_m2s3": 924.412988},
  "by_gate": {"tool_on": {"...same fields"}, "gripped": {"...same fields"}},
  "grip_unknown_s": 26.300007, "not_gripping_s": 0.0,
  "current": {"a_hv_ms2": 1.2823, "tool_now": true, "grip_activation": null, "gripping": null, "counting": true,
              "points_per_hour": 3.288587, "hours_to_eav": 30.252035, "hours_to_limit": 121.476637},
  "limits": {"eav_ms2": 2.5, "elv_ms2": 5.0, "eav_points": 100.0, "elv_points": 400.0,
             "personal_limit_points": 400.0, "personal_limit_lowered": false},
  "grip_during_contact": {"seconds": 0.0, "mean": null, "p10": null, "p50": null, "p90": null,
                          "units": "normalised activation (0 relaxed, 1 calibration squeeze)", "reference": {"...": "Jonsson"}},
  "years_to_10pct_vwf": {"value": null, "basis": "...", "note": "ISO 5349-1 Annex C group estimate ..."},
  "weighting": "none (unweighted; over-states high-frequency tools vs ISO W_h)",
  "measurement": "wrist-worn IMU monitoring estimate (ISO/TS 22270 style), not ISO 5349-2",
  "accumulation": 1, "boot_segments": 1, "integrated_until_us": 56604000,
  "no_tool_s": 27.399987, "degraded_duration_s": 0.0, "excluded_duration_s": 1.150012, "excluded_by_reason": {"...": "..."},
  "clock": "device_time ...", "formula": {"a8": "...", "points": "...", "coefficients": "{ExposureConfig}"}
}
```

| Field | Meaning |
|---|---|
| `daily` | the headline accumulation (`gate`: `gripped` by default, `tool_on` if `grip_gate=False`) |
| `daily.points` | HSE points so far today; 100 = action value, 400 = limit |
| `daily.a8_ms2` | A(8) estimate in m/s² |
| `daily.energy_m2s3` | Σ a² dt, the quantity that adds up across sessions and tools |
| `by_gate.tool_on` | every second the tool detector said "tool" (vibration-only count) |
| `by_gate.gripped` | `tool_on` minus seconds the wearer was known to be relaxed (unknown grip still counts) |
| `level` | `below_eav` / `above_eav` / `above_limit` (limit = personal limit) |
| `current.counting` | true when this instant adds to the headline (tool on and not known relaxed) |
| `current.hours_to_eav` / `hours_to_limit` | time left at the current intensity; `0` = reached, `null` = no exposure right now |
| `status` / `reasons` | `paused` when the vibration or tool-detector input is missing/stale/warming up |

The accumulation is a **shift**: `POST /api/runtimes/{name}/tool-use/new-shift` now also resets it (the
`tool_session_reset` event detail gets `exposure_points`); a provenance change resets it too. A boot change
keeps the day.

### New endpoints

| Method, path | Request | Response |
|---|---|---|
| `GET /api/exposure/limits` | | values, formulas, worked example (6 m/s² → 72 points/h, 1.39 h / 5.56 h), years-to-risk examples, Jonsson reference, sources, limitations |
| `GET /api/exposure/daily?days=14` | | `{days: [{date, source, sessions, gate, points, a8_ms2, contact_s, percent_of_limit, level, by_gate}], rolling_7_day: {<source>: {days_with_exposure, a8_ms2, years_to_10pct_vwf}}, personal_limit_points, ...}`. Day = local date of the session start. **Sources are never added together.** |
| `GET /api/exposure/personal-limit` | | `{personal_limit_points: 400.0, updated_at, lowered, equivalent_a8_ms2, note}` |
| `PUT /api/exposure/personal-limit` | `{"personal_limit_points": 100}` | same + `applied_to_runtimes`; 400 if not in (0, 400] (it can only be lowered). Saved in `<data_dir>/exposure_settings.json` |

### Session / history records

`counters["exposure"]` on each wearable session row (JSON column that already exists, **no schema change**):
`{status, reason, gate, by_gate: {tool_on, gripped: {energy_m2s3, contact_s, points, a8_ms2}}, energy_m2s3,
contact_s, points, a8_ms2, grip_unknown_s, grip_during_contact, label, note}`. Old sessions have no
`exposure` key: they still load, and the daily view skips them.

### Dashboard

- Live: new card `#ro-exposure` above the three readouts. The old score card is still there, renamed
  "Legacy score". Remove it if the team agrees.
- New tab `#tab-limits` / `#view-limits` (static HTML, so it works on the mock feed and the landing demo).
- Hidden, not deleted: `#tab-station`, `#dev-station`, Overview "Perception test answers" and "Perception
  threshold per test". Remove `hidden` to bring them back (and put `'station'` back in `VIEWS`).
- `contract.js` not changed: the exposure message is the backend record itself (no translation).
- Not done: a daily points chart on History/Overview (the data is ready at `/api/exposure/daily`).

### Android

Nothing changed in `android/` or `mobile/`: both open the backend's dashboard, so they get the new card and
tab after a backend restart. No rebuild needed.

### iOS (not changed)

To show it in SwiftUI: in `Backend.swift` add `case ("exposure", _): exposure = p` next to the
`experimental_score` case; in `Translate.swift` read `daily.points`, `daily.a8_ms2`, `level`,
`current.points_per_hour`, `current.hours_to_eav`, `current.hours_to_limit`, `by_gate.gripped.contact_s`,
`by_gate.tool_on.contact_s`, `limits.personal_limit_points`; add a Limits screen from `GET /api/exposure/limits`.

### Things to do or decide

- Review and merge this branch into `android-app` (or wherever integration goes next).
- Decide whether to remove the "Legacy score" card and the score charts on Overview.
- Optional: a daily points chart from `/api/exposure/daily`.
- Removed or deprecated: **nothing**.

## 3. For Salaar (signals, firmware)

| File | Change |
|---|---|
| `signals/src/vibeguard_signals/processing/exposure.py` | **New**: `ExposureEngine`, `wh_gain`, `a8_from_energy`, `points_from_energy`, `hours_until`, `years_to_10pct_vwf`, `limits_reference` |
| `signals/src/vibeguard_signals/processing/config.py` | **New** `ExposureConfig` + `WEIGHTINGS`; `ProcessingConfig.exposure` field; `validate()` calls it |
| `signals/src/vibeguard_signals/processing/processor.py` | additive: builds `self.exposure`; feeds it activation (EMG), vibration + tool record (IMU); emits `exposure` after `experimental_score` and in `finish()`; boot change → flush; provenance change → reset + `exposure_accumulation_reset` event; `tool_use_reset_session()` also resets exposure; new `set_personal_exposure_limit()` |
| `signals/tests/test_exposure.py` | **New**: 40 tests |

- `score.py` is **not modified**: `exposure.py` imports its `_Track`, `_Input`, `_block_key` and `USABLE`
  so the interval rules (freshness, stale gaps, duplicates, boot flush) are exactly the same. If you
  rename those, exposure.py needs the same rename.
- Defaults: `eav 2.5`, `elv 5.0`, `t0 8 h`, `grip_gate True`, `grip_activation_min 0.10`, personal limit 400,
  `weighting "none"`, freshness 0.25 s for vibration, tool and activation.
- **Please decide on weighting.** Unweighted a_hv over-states high-frequency tools a lot compared with ISO
  W_h (|W_h| ≈ 0.13 at 125 Hz, 0.10 at 160 Hz, so points can be ~100× too high for a 160 Hz motor). I added
  `weighting="wh_dominant_frequency"` (a_hv × |W_h| at the tool detector's dominant frequency, a
  single-frequency approximation). Default stays `none` until you agree; a proper W_h filter in
  `vibration.py` would be better.
- Firmware request (stretch, not done): a haptic buzz at 100 points and at the personal limit (DRV2605L).

## 4. Formulas and limits

```
a_hv   = sqrt(ax² + ay² + az²)                 combined dynamic accel RMS (vibration.combined_rms)
A(8)   = sqrt( Σ a_hv,i² · Δt_i / T0 ),  T0 = 8 h      ISO 5349-1 multi-operation form
points = 2 · a² · hours = 100 · (A(8) / 2.5)²          HSE
time to level = (level − points) / (2 · a_now²) hours
years  = 31.8 · A(8)^−1.06                             ISO 5349-1 Annex C, 10 % of a group; not shown below 1 m/s²
```

Action value 2.5 m/s² (100 points), limit value 5.0 m/s² (400 points): EU Directive 2002/44/EC; UK
Control of Vibration at Work Regulations 2005 (Schedule 1) and HSE guidance. ACGIH uses similar values;
Alberta has no numeric HAV limit. Sources are listed on the Limits page and in `exposure.SOURCES`.

## 5. How to run and test

```bash
cd signals && uv run pytest -q
cd backend && uv run pytest -q
node backend/tests/dashboard_adapter_check.js
node backend/tests/dashboard_overview_check.js
```

- Mock feed: open `dashboard/index.html` (or `/dashboard/?source=mock`), press Connect, tick "Tool running".
- Simulated backend: `uv run --project backend vg-backend serve --scenario tool_vs_arm --duration 60`, then
  open http://127.0.0.1:8765/dashboard/ (the tool phases start about 20 s in).
- Real wearable: the usual `serve --device wifi`; the card fills once the tool detector says "tool".

## 6. Test results

Run on Windows 11 (Python 3.13, uv). Baseline = commit `36f51af` (this branch before my commits).

| Suite | Baseline | This branch |
|---|---|---|
| signals | 328 passed, 2 failed (on `ios-app`) | **374 passed**, 2 failed (40 new tests) |
| backend new tests (`test_exposure_api.py`) | n/a | **7 passed** |
| backend, the files that changed | | `test_gaps_and_disconnects`, `test_live_delivery`, `test_exposure_api`: 42 passed |
| `dashboard_adapter_check.js`, `dashboard_overview_check.js` | pass | **pass** |

- The 2 signals failures (`test_tracked_recordings_regenerate_exactly`) fail on the baseline too: the
  checkout converts the fixture files to CRLF on Windows (`core.autocrlf=true`). Not related to this change.
- Backend on Windows, same on baseline and this branch: 5 test files cannot load (`termios` / `resource`
  are Linux-only), the same 15 tests fail or error (`test_dashboard_adapter::test_backend_adapter_translation`,
  `test_http_review::test_websocket_ignores_a_binary_frame`, 4 x `test_landing_page`, 6 x
  `test_persistence_restart`, `test_profile_review::test_backup_overrides_device...`,
  `test_regressions::test_second_server_on_same_data_dir...`), and the full run hangs at
  `test_runtime_review::test_a_failed_periodic_flush_does_not_stop_acquisition` and in
  `test_selector_stream_review`. Please run the backend suite on Linux/macOS, where it was written.
- One test changed on purpose: `test_gaps_and_disconnects` checked the exact keys of `latest`; it now
  includes `"exposure": None`.
- Checked by hand in the browser: the card and the Limits page on the simulated backend
  (`tool_vs_arm`: 6.95 m/s^2 -> 97 points/h, 1 h 02 min to the action value) and on the mock feed, and the
  phone layout at 375 px (no horizontal scroll).

## 7. Merge notes

- Base: `android-app` `4f73e5e` + `firmware-esp32` `0da519b` (a clean merge, commit `36f51af`). The PR
  targets `android-app`, so it also brings Salaar's two `firmware-esp32` commits (tool calibration) with it.
- Likely conflicts: `dashboard/index.html`, `dashboard/js/app.js`, `dashboard/css/style.css`,
  `backend/.../runtime.py`, `service.py`, `app.py` if they change before this merges (all my edits are small
  insertions; keep both sides). `signals/` is low risk.
- `ios-app` is not changed; `main` is behind `android-app`.

## 8. Known limitations / not done

- Estimate only: wrist-worn hobby IMU (500 Hz, accel low-pass ~218 Hz), unweighted by default.
- While the tool detector holds "tool" for its off-hold time (~3 s) after a tool stops, those seconds count
  as contact at near-zero vibration (energy is negligible; contact time is slightly high).
- Not done: daily points chart on History/Overview, years-to-risk tile, iOS screens, weekly check-ins,
  CSV report, shift start with worker name, per-tool attribution, alerts log, supervisor compare view,
  haptic alerts. The personal limit is one value per backend (no worker profiles yet).

## 9. Commit list

```
04168ef Backend test: runtime latest now includes the exposure slot (None before any data)
accb537 Dashboard: daily exposure card, Limits page, perception test hidden
0bca37b Backend: publish exposure records, per-session exposure, daily totals, personal limit
487a46b Signals: daily vibration exposure estimate (ISO 5349-1 A(8), EU/UK limits, HSE points)
(+ this report)
```
