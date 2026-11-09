// Browser ES module. Uses original derived records; no invented UI measurements.
// Call the returned cleanup function when unmounting the view.
export async function playMock(onRecord, {baseUrl = "http://127.0.0.1:8766", speed = 1} = {}) {
  if (!Number.isFinite(speed) || speed <= 0) throw new Error("speed must be positive");
  const response = await fetch(`${baseUrl}/derived.json`);
  if (!response.ok) throw new Error(`Mock feed HTTP ${response.status}`);
  const bundle = await response.json();
  if (bundle.source !== "simulated" || bundle.hardware_verified !== false)
    throw new Error("Expected explicitly simulated fixture");
  let cancelled = false;
  const started = performance.now();
  // This fixed demo has one boot. Invalidations may carry an earlier sample time.
  let targetMs = 0;
  (async () => {
    for (const record of bundle.records) {
      targetMs = Math.max(targetMs, (record.t_us ?? 0) / 1000 / speed);
      await new Promise(resolve => setTimeout(resolve, Math.max(0, started + targetMs - performance.now())));
      if (cancelled) return;
      onRecord(record);
    }
  })();
  return () => { cancelled = true; };
}
