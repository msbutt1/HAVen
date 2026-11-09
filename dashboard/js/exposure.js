/*
 * Daily vibration exposure card (Live tab) and the personal limit on the Limits tab.
 *
 * Shows the processor's "exposure" record (signals/processing/exposure.py; backend-adapter.js forwards it,
 * mock-adapter.js simulates the same shape): HSE points today on a bar with the action value (100) and the
 * limit (400, or a lowered personal limit), the A(8) estimate, points per hour now, and the time left until
 * each level at the current intensity. Grip is shown as contact time, not as a multiplier.
 *
 * Colours mean something here, unlike the Overview: these ARE regulatory reference values (EU Directive
 * 2002/44/EC, UK CVWR 2005). Green below the action value, amber above it, red at the limit. The text always
 * says it is an estimate from a wrist sensor; below the action value is not "safe".
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});
  const $ = (id) => document.getElementById(id);
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const EAV = 100;
  const ELV = 400;

  // ---------- the formulas (same as exposure.py; the mock adapter uses these too) ----------
  const math = {
    pointsFromEnergy: (e2) => (2 * Math.max(0, e2)) / 3600, // HSE: 2 * a^2 * hours
    a8FromEnergy: (e2) => Math.sqrt(Math.max(0, e2) / 28800), // A(8) = sqrt(sum a^2 dt / 8 h)
    hoursUntil(points, level, aNow) {
      if (points >= level) return 0;
      if (!isNum(aNow) || aNow <= 0) return null;
      return (level - points) / (2 * aNow * aNow);
    },
  };
  VG.ExposureMath = math;

  function duration(h) {
    if (h == null) return '—';
    if (h === 0) return 'reached';
    if (h > 24) return 'over 24 h';
    const m = Math.round(h * 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
  }
  const minutes = (s) => (!isNum(s) ? '—' : s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

  const LEVEL_TEXT = {
    below_eav: 'Below the action value. Keep tool time planned and take breaks.',
    above_eav: 'Above the action value: exposure must be reduced (rotate jobs, lower-vibration tools, breaks).',
    above_limit: 'Daily limit reached. Stop using vibrating tools for today.',
  };

  function render(rec) {
    const card = $('ro-exposure');
    if (!card || !rec) return;
    const daily = rec.daily || {};
    const limits = rec.limits || {};
    const limit = isNum(limits.personal_limit_points) ? limits.personal_limit_points : ELV;
    const pts = isNum(daily.points) ? daily.points : null;
    const cur = rec.current || {};
    const sim = (rec.provenance && rec.provenance.source === 'simulated') || rec.simulated === true;

    card.dataset.level = pts == null ? 'none' : rec.level || 'below_eav';
    card.classList.toggle('is-stale', rec.status !== 'accumulating');
    $('exp-sim').hidden = !sim;
    $('exp-points').textContent = pts == null ? '—' : pts < 10 ? pts.toFixed(1) : String(Math.round(pts));
    $('exp-limit-label').textContent = limit < ELV ? `${Math.round(limit)} = your personal limit` : '400 = limit';

    // bar: 0..max(limit, points) with a little headroom; markers at 100 and the limit
    const span = Math.max(limit, EAV, pts || 0) * 1.05;
    $('exp-fill').style.width = `${Math.min(100, ((pts || 0) / span) * 100)}%`;
    $('exp-mark-eav').style.left = `${(EAV / span) * 100}%`;
    const lm = $('exp-mark-limit');
    lm.style.left = `${(limit / span) * 100}%`;
    lm.innerHTML = `<b>${Math.round(limit)}</b> ${limit < ELV ? 'your limit' : 'limit'}`;
    $('exp-mark-eav').hidden = limit <= EAV;

    let level = LEVEL_TEXT[rec.level] || '';
    if (rec.status !== 'accumulating') level = `Paused: ${(rec.reasons || []).join(', ') || 'no data'}. ${level}`;
    $('exp-level').textContent = level;

    $('exp-a8').textContent = isNum(daily.a8_ms2) ? `${daily.a8_ms2.toFixed(2)} m/s²` : '—';
    const counting = cur.counting === true;
    $('exp-pph').textContent = counting && isNum(cur.points_per_hour) ? `${cur.points_per_hour.toFixed(0)} /h`
      : cur.tool_now === false ? 'no tool' : '—';
    $('exp-to-eav').textContent = pts != null && pts >= EAV ? 'reached' : counting ? duration(cur.hours_to_eav) : '—';
    $('exp-to-limit').textContent = pts != null && pts >= limit ? 'reached' : counting ? duration(cur.hours_to_limit) : '—';

    const g = rec.by_gate || {};
    const gripped = g.gripped && g.gripped.contact_s;
    const toolOn = g.tool_on && g.tool_on.contact_s;
    const unknown = isNum(rec.grip_unknown_s) && rec.grip_unknown_s > 1 ? `, grip unknown ${minutes(rec.grip_unknown_s)}` : '';
    $('exp-grip').textContent = `Gripped contact: ${minutes(gripped)} (tool on: ${minutes(toolOn)}${unknown})`;

    const lp = $('lim-personal');
    if (lp) lp.textContent = `${Math.round(limit)} points${limit < ELV ? ' (lowered)' : ''}`;
  }
  VG.renderExposure = render;

  // the backend sends a record per feature window (~30/s): draw at most 4 times a second, always the newest
  let pending = null;
  let timer = null;
  function schedule(rec) {
    pending = rec;
    if (timer) return;
    timer = setTimeout(() => { timer = null; render(pending); }, 250);
  }
  if (VG.adapter && typeof VG.adapter.on === 'function') VG.adapter.on('exposure', schedule);
})();
