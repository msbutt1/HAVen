# HAVen: shared project brief

## Objective
Build a reliable hackathon prototype combining forearm muscle activity, wrist vibration, a fingertip vibration-perception workflow, and a local dashboard. Demonstrate a complete measurement-to-record workflow rather than clinical accuracy.

## Time and parallelism
Assume 50 elapsed hours and five people: 250 nominal person-hours, with roughly 150–175 productive hours after rest, meals, coordination, and rehearsal. These are planning estimates. Stagger rest; milestone hours are not continuous-work expectations.

Five parallel workstreams: Collins hardware; Salaar firmware/signals with limited hardware support; abduljabbar backend; Ali frontend; Fahad station software/integration. Ali/Fahad allocation is a planning assumption based on the available flexible roles.

## Existing sleeve
A tattoo-style sleeve is already available. No nylon purchase, sewing task, sewn conductive electrodes, or custom dry-electrode work. Start with gel electrodes against skin; use the sleeve to secure components and route cables. Verify fit and stability.

## Must ship
- One wearable with EMG and a secured IMU.
- Relaxed/squeeze calibration and normalized muscle-activation display.
- Live vibration measurement within actual sensor capabilities.
- Separately labeled experimental exposure score.
- Motor stimulus and felt/not-felt response workflow.
- Dashboard with live session, stored results, and labeled simulated history.
- Recorded backup demo and reproducible local startup.

## Stretch only
Ambient-temperature flag; OLED; second IMU; reliable contact-pressure gating; enclosure polish; extra worker comparisons.

## Out of scope
Bluetooth/mobile app, adaptive cancellation, custom electrodes, production authentication/cloud infrastructure, clinically meaningful diagnostic cutoffs, validated grip-force estimation, and claims of standards-compliant dosimetry.

## Scientific framing
Keep measured vibration, normalized muscle activity, and experimental combined score separate. Increasing a score with grip demonstrates the chosen algorithm, not validated physical transmission. The station is a vibration-perception prototype; it does not establish early nerve-damage detection. Motor settings are not measured physical amplitudes. Cold is an optional contextual flag, not a validated dose multiplier. Historical demo data is simulated and labeled.

## Source context
Adapted from the supplied “natHacks 2026 — HAVen HAVS Dose + Nerve-Check Wearable-2.pdf” and the subsequent 50-hour planning discussion. The PDF's original 64-hour schedule, sewing tasks, and stronger medical/product claims are superseded by this plan. Its epidemiology, prices, competition claims, and standards references need independent verification before use in slides. These files are an implementation plan, not a verbatim PDF transcription.
