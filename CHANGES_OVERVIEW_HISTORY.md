# Overview over time (Today / Week / Month / Year) + sample history

Branch `fahad/overview-history`, based on `origin/firmware-esp32` (4953882). Android standalone app only.

## 1. Summary

- The Overview tab now has a range selector: **Today** (by hour), **Week** (7 days), **Month** (30 days) and **Year** (12 months). The Points / Tool time toggle is unchanged.
- **Limits stay daily.** Week and Month show one bar per day. Year shows one bar per month: the energy-mean daily A(8) of that month's working days, shown as average points per working day. Days are never summed.
- The y axis is capped at `max(limit × 1.15, 95th percentile × 1.1)`, so one demo-speed day (for example 557 points) no longer flattens the rest. Bars taller than the cap reach the top with a "▲557" label.
- Tapping a bar shows its details under the chart.
- The stats tiles, the average card and the day list all follow the selected range. Month and Year add a trend line, for example "Your average is down 48% vs the first half of the year".
- New card, **What the rules suggest**: guidance based on EU 2002/44/EC and UK HSE, using the ISO 5349-1 method. It links to How it works.
- History now keeps **400 days** (was 30). Each archived day also stores `contact` and, if it was recorded at demo speed, `demoSpeed`. Demo-speed days are labelled.
- **Sample year** for demos: `history:sample`, `history:clearSample` and `history:resetDemo`. The days are deterministic and labelled, and they never overwrite a real day or touch today.

## 2. For Salaar (Android standalone app owner)

### Files and functions touched

| File | Change |
|---|---|
| `android/.../app/History.java` (**new**) | Pure Java with no Android classes: `trim`, `clearSample`, `addSample`/`sampleDay`, `months` (12-month energy mean), plus A(8)/points helpers. |
| `android/.../app/Sleeve.java` | See the list below this table. |
| `android/app/src/main/assets/standalone/index.html` | Overview markup, overview JS and CSS, a Developer options card, and one sentence in How it works. |
| `android/app/src/test/.../app/HistoryTest.java` (**new**) | 6 tests. |

Changes in `Sleeve.java`:
- **New fields:** `dayDemo`, `dayHours[24]`, and a history cache (`historyVersion`, `historyBuilt`, `historyDays`, `historyJson`).
- **`loadDay` / `saveDay`:** also read and write `demoSpeed` and `hours`.
- **`archive(date, energy, tool, contact, demo)`:** two new parameters. It writes through the new `saveDays()`.
- **New helpers:**
  - `saveDays()` trims to 400 days and bumps `historyVersion`.
  - `historyJson()` caches the history.
  - `resetToday()`.
- **`logDay`:** adds energy into the current hour's slot and records the day's demo speed.
- **`tick()`:** the `history` array is spliced into the state string from the cache, instead of being re-parsed from prefs and re-serialised 20×/s.
- **`onCommand`:**
  - Three new commands.
  - `day:clear` uses `resetToday()`.
  - `week:sample` / `week:clearSample` behave the same as before; they now only save through `saveDays()`.
- **`build()`:** the `history` block is replaced by `historyVersion`, `todayHours`, `dayDemoSpeed` and `months`.

### New commands

| Command | Effect |
|---|---|
| `history:sample` | Removes the old sample days, then fills the 365 days before today. |
| `history:clearSample` | Removes every sample day, including the ones from `week:sample`. Real days stay. |
| `history:resetDemo` | Clears today (like `day:clear`), then removes and re-adds the sample year. |

The existing `week:sample`, `week:clearSample`, `day:clear`, `speed:` and `limit:` commands are unchanged. Their Developer options buttons are still there.

### State fields

| Field | Status |
|---|---|
| `history` | Same shape: newest first, with `date`. It now covers up to 400 days, and entries may carry `contact`, `demoSpeed` and `sample`. |
| `historyVersion` (new) | Increments on every write to the day log. The page uses it to skip redraws. |
| `todayHours` (new) | 24 numbers: points added in each hour of today. |
| `dayDemoSpeed` (new) | The highest demo speed that added points today (0 if none). |
| `months` (new) | The last 12 calendar months, oldest first, with fields `month`, `days`, `a8`, `points` (average per working day), `tool` (average seconds per working day), `toolTotal`, `overAction`, `overLimit`, `sample` and `demo`. The current month includes today's live reading. |

### Storage and backward compatibility

- Same `SharedPreferences` keys (`"today"`, `"days"`). There is no migration.
- Old days without `a8`, `contact` or `demoSpeed` still load: A(8) falls back to `2.5·√(points/100)`.
- An old `"today"` without `hours` or `demoSpeed` starts those at 0.
- Size: about 100 bytes per day, so about 40 KB at 400 days.

### Sample year (deterministic, seeded per date)

- **Working pattern:** weekdays are worked (4% sick days). About 15% of Saturdays and 3% of Sundays are worked. Fixed holidays are off: Jan 1–2, Jul 1, Nov 11 and Dec 24–31.
- **The story:** median 125 points in the older half, then a one-month change-over (195 to 165 days ago) to a median of 65. Spread is log-normal (σ = 0.6), clamped to 5–520.
- **Tool time:** derived from a per-day tool vibration of 4–12 m/s² as `hours = points / (2a²)`, capped at 8 h.
- **Stored per day:** `a8 = 2.5·√(points/100)`, `limit = 400`, `sample: true`.
- **Measured (HistoryTest output for 2026-11-08):**
  - 256 work days, 6 of them on weekends
  - 109 over 100 points (43%)
  - 2 over 400
  - older half average 136, recent half average 77
  - The Year view reads "down 48%".
- **Deviation from the plan:** the plan asked for 15–25% of days over 100 *and* a first-half average of about 180. Both can't hold at once, so I kept the story: the first half has more days over 100 and the second half about 20%.

## 3. For Abduljabbar

- **No change** to the backend, the laptop dashboard, firmware or iOS. Nothing outside `android/` was changed except this report.
- The year math matches the backend's `exposure.py daily_exposure`: energy-mean A(8) over the exposed days.
- **iOS parity would need:**
  1. Keep 400 days in its day log, and store `contact` and `demoSpeed` on each day.
  2. A port of `History.java` (the sample generator and `months`).
  3. The same three commands.
  4. `todayHours`, `dayDemoSpeed` and `months` in its state.
  5. The new Overview HTML/JS. If iOS uses the same `standalone/index.html`, copy it over.

## 4. How to demo

1. Settings → Developer options → **Reset demo** (confirm). Today goes to 0, and 12 months of sample days are filled in.
2. Back to **Today**. Run the massage gun with demo speed on (default: 1 minute counts as 8 hours) and watch the ring pass the action level.
3. **Overview → Week**: sample days are faded, and today is live.
4. **Month**: 30 bars and the trend line. Tap a bar for that day's details.
5. **Year**: 12 monthly bars stepping down after the change-over, "Your average is down 48%…", and the years-to-risk group estimate.
6. Scroll to **What the rules suggest**, then tap **Why these rules?**
7. Run it again: **Reset demo**.

## 5. How to test, and results

```bash
cd android
JAVA_HOME="/c/Program Files/Android/Android Studio/jbr" ./gradlew testDebugUnitTest assembleDebug
```

- **Unit tests, run on Windows with Android Studio's JBR:** all 14 pass.
  - `ConnectTest`: 5
  - `RecordingTest`: 3
  - `HistoryTest` (new): 6. It covers:
    - retention: keeps 400 days and drops older ones
    - the generator is deterministic and marks its days as sample
    - it never touches today and never overwrites real days
    - clearing removes sample days only
    - the distribution is plausible and the story holds
    - the monthly energy mean is not a sum (100/400/25 → 175 points per working day)
    - entries without `a8` still count
- `assembleDebug` builds `app-debug.apk`.
- **UI check:** the page ran in a browser at 375 px against a mocked `VG` bridge.
  - All four ranges, bar taps, Show more and the rules card work.
  - No horizontal scroll.
  - A Year render takes about 5 ms.
- **Not tested on a phone yet.**

## 6. Merge notes

- Base: `firmware-esp32`. The PR targets `firmware-esp32`.
- Expected conflicts:
  - only if Salaar has since changed `Sleeve.java` (`archive`, `logDay`, `onCommand`, `build`, `tick`), or
  - the Overview section or Developer options of `index.html`.
- Everything else is additive.

## 7. Known limitations / not done

- Not tested on a real phone.
- iOS not updated.
- The demo-speed flag only applies to days archived after this change. Older demo days, such as an earlier 557-point day, show without the tag, but the axis cap still handles them.
- "Today by hour" buckets use the phone's local hour. Points recorded before this update are not split by hour.
- Week and Month averages are computed in JS. The Year months are computed in Java. Both use the same formula.
- The guidance is general information from the rules, not advice for a specific person.

## 8. Commits

`git log --oneline origin/firmware-esp32..HEAD` (before this report's own commit):

```
00acf05 Android Overview: Today / Week / Month / Year, capped axis, stats, trend and what the rules suggest
98aa7af Android: sample year for demos (history:sample, history:clearSample, history:resetDemo) and tests
cea8a7a Android: keep 400 days of history, store demo speed and contact on each day, today by hour
```
