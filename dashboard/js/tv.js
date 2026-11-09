/* HAVen TV meters. Reads the backend's live WebSocket (raw telemetry + derived features) from the same origin.
 *
 * Grip  = the processor's calibrated grip effort (% of this wearer's max squeeze). Empty until calibrated.
 * Vibration = measured wrist vibration (m/s², unweighted RMS) as a % of VIB_MAX (default 10 m/s², ?vibmax=).
 *
 * "LIVE" is shown only for the real wearable: the runtime's adapter is wifi or serial, the latest record's
 * provenance is "live", and data arrived under STALE_MS ago. Simulated, replayed or recorded data is named so.
 */
(function () {
  'use strict';

  const q = new URLSearchParams(location.search);
  const RUNTIME = q.get('runtime') || 'wearable';
  // phone: served by the Android app (TvServer), which reads the sleeve and computes everything itself
  const MODE = q.get('source') === 'phone' ? 'phone' : 'backend';
  const VIB_MAX = Math.max(0.1, Number(q.get('vibmax')) || 10);
  const STALE_MS = 1500;
  const RATE_WINDOW_MS = 3000;
  const PEAK_HOLD_MS = 1200;

  const $ = (id) => document.getElementById(id);
  const el = {
    badge: $('badge-text'),
    sGrip: $('s-grip'), sVib: $('s-vib'),
    trackGrip: $('track-grip'), trackVib: $('track-vib'),
    fillGrip: $('fill-grip'), fillVib: $('fill-vib'),
    peakGrip: $('peak-grip'), peakVib: $('peak-vib'),
    ovGrip: $('ov-grip'),
    pctGrip: $('pct-grip'), pctVib: $('pct-vib'),
    dGrip: $('d-grip'), dVib: $('d-vib'),
    device: $('pf-device'), emgRate: $('pf-emg'), imuRate: $('pf-imu'), gaps: $('pf-gaps'), age: $('pf-age'),
    btnRelaxed: $('btn-relaxed'), btnSqueeze: $('btn-squeeze'),
    toast: $('toast'),
    gapsLabel: $('pl-gaps'), calBtns: $('cal-btns'),
  };

  // ---------------------------------------------------------------- state
  const S = {
    wsOpen: false,
    status: null,          // runtime status (adapter, connection, data_fresh, calibration, ...)
    hello: null,
    provenance: null,      // provenance of the latest record
    lastRx: 0,             // performance.now() of the latest telemetry/derived message
    calState: 'uncalibrated',
    capturing: null,
    grip: { target: null, shown: 0, peak: 0, peakAt: 0, text: null },
    vib: { target: null, shown: 0, peak: 0, peakAt: 0, ms2: null },
    rate: { emg: [], imu: [] },               // [t, samples] per batch, last RATE_WINDOW_MS
    next: { emg: null, imu: null, boot: null }, // expected first_sample_index per stream
    gaps: 0,
    phone: null,           // the phone's latest snapshot (phone mode)
    phoneSamples: null,    // previous cumulative sample counts, to turn them into rates
  };

  // ---------------------------------------------------------------- helpers
  const first = (v) => (v && typeof v === 'object') ? Object.values(v)[0] : v;
  const num = (v) => (typeof v === 'number' && isFinite(v)) ? v : null;
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  let toastTimer = 0;
  function toast(msg, ms = 3000) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms);
  }
  function shortModel(s, cut) {
    if (typeof s !== 'string' || !s) return null;
    const i = s.indexOf(cut);
    return (i > 0 ? s.slice(0, i) : s).trim();
  }

  // ---------------------------------------------------------------- incoming
  function onMessage(m) {
    if (!m || typeof m.channel !== 'string') return;
    if (m.channel === 'snapshot') {
      const rt = ((m.payload && m.payload.runtimes) || []).find((r) => r.runtime === RUNTIME);
      if (!rt) { S.status = null; toast(`The laptop has no device called "${RUNTIME}".`, 0); return; }
      S.status = rt;
      if (rt.hello) onHello(rt.hello);
      if (rt.provenance) S.provenance = rt.provenance;
      const lat = rt.latest || {};
      if (lat.emg) onFeatures(lat.emg);
      if (lat.imu) onFeatures(lat.imu);
      if (rt.data_fresh) S.lastRx = performance.now();
      return;
    }
    if (m.runtime !== RUNTIME) return;
    const p = m.payload || {};
    switch (m.channel) {
      case 'telemetry': onTelemetry(p); break;
      case 'derived':
        S.lastRx = performance.now();
        if (p.provenance) S.provenance = p.provenance;
        if (p.type === 'stream_features') onFeatures(p);
        break;
      case 'device':
        if (p.type === 'hello') onHello(p);
        if (p.provenance) S.provenance = p.provenance;
        break;
      case 'runtime':
        if (p.runtime === RUNTIME && p.state) S.status = p;           // full status push
        else if (p.event === 'device_silent') S.lastRx = 0;
        break;
      default: break;
    }
  }

  function onHello(h) {
    S.hello = h;
    const st = h.streams || {};
    el.sGrip.textContent = shortModel(st.emg && st.emg.sensor_model, ' via') || 'EMG · forearm';
    el.sVib.textContent = shortModel(st.imu && st.imu.sensor_model, ',') || 'Accelerometer · wrist';
    el.device.textContent = h.device_id || '--';
  }

  function onTelemetry(p) {
    const now = performance.now();
    S.lastRx = now;
    if (p.provenance) S.provenance = p.provenance;
    const stream = p.type === 'emg_batch' ? 'emg' : p.type === 'imu_batch' ? 'imu' : null;
    if (!stream || !Array.isArray(p.samples)) return;
    const n = p.samples.length;
    S.rate[stream].push([now, n]);
    // gap check on the device's own sample counter (reset when the board reboots)
    if (p.boot_id !== S.next.boot) { S.next = { emg: null, imu: null, boot: p.boot_id }; }
    const idx = num(p.first_sample_index);
    if (idx != null) {
      const exp = S.next[stream];
      if (exp != null && idx > exp) S.gaps += idx - exp;
      S.next[stream] = idx + n;
    }
  }

  function onFeatures(p) {
    const f = p.features || {};
    if (p.stream === 'emg') {
      const act = f.emg_activation || {};
      const norm = act.normalized || {};
      const cal = act.calibration || {};
      S.calState = cal.state || S.calState;
      S.capturing = cal.capturing || null;
      const ok = norm.status === 'ok' || norm.status === 'degraded';
      const val = ok ? num(first(norm.value)) : null;
      const disp = ok ? num(first(norm.display)) : null;
      S.grip.target = disp == null ? null : clamp01(disp);
      S.grip.text = val == null ? null : Math.max(0, Math.round(val * 100));
    } else if (p.stream === 'imu') {
      const phys = (f.vibration && f.vibration.physical) || {};
      const v = phys.status === 'ok' ? num(phys.combined_rms) : null;
      S.vib.ms2 = v;
      S.vib.target = v == null ? null : clamp01(v / VIB_MAX);
    }
  }

  // ---------------------------------------------------------------- phone mode
  function onPhone(p) {
    const now = performance.now();
    S.phone = p;
    S.status = p;
    S.lastRx = p.age == null ? 0 : now - p.age * 1000;
    if (p.device) el.device.textContent = p.device;
    if (p.emgSensor) el.sGrip.textContent = shortModel(p.emgSensor, ' via');
    if (p.imuSensor) el.sVib.textContent = shortModel(p.imuSensor, ',');
    // cumulative sample counts -> per-message deltas, so rate() works as with raw batches
    const prev = S.phoneSamples;
    if (prev && p.emgSamples >= prev.emg && p.imuSamples >= prev.imu) {
      if (p.emgSamples > prev.emg) S.rate.emg.push([now, p.emgSamples - prev.emg]);
      if (p.imuSamples > prev.imu) S.rate.imu.push([now, p.imuSamples - prev.imu]);
    }
    S.phoneSamples = { emg: p.emgSamples, imu: p.imuSamples };
    S.calState = p.gripState || 'uncalibrated';
    S.capturing = typeof S.calState === 'string' && S.calState.startsWith('capturing_') ? S.calState.slice(10) : null;
    const act = num(p.activation);
    S.grip.target = act == null ? null : clamp01(act);
    S.grip.text = act == null ? null : Math.max(0, Math.round(act * 100));
    const v = num(p.vibration);
    S.vib.ms2 = v;
    S.vib.target = v == null ? null : clamp01(v / VIB_MAX);
  }

  function sourcePhone(now) {
    const p = S.phone;
    if (!S.wsOpen || !p) return ['none', 'Not connected to the phone'];
    if (p.source === 'demo' || p.dataSource === 'simulated') return ['simulated', 'Simulated · phone practice mode'];
    if (p.dataSource === 'replay') return ['replay', 'Replay · recorded, not live'];
    const fresh = S.lastRx && now - S.lastRx < STALE_MS;
    if (!p.running || !p.connected || !fresh) return ['stale', 'No signal · sleeve not sending'];
    if (p.dataSource === 'live') return ['live', 'Live · sleeve → phone'];
    return ['stale', 'Source unknown'];
  }

  // ---------------------------------------------------------------- source badge
  function source(now) {
    if (MODE === 'phone') return sourcePhone(now);
    if (!S.wsOpen) return ['none', 'Not connected to the laptop'];
    const st = S.status;
    if (!st) return ['none', 'No device on the laptop'];
    const kind = (st.adapter && st.adapter.kind) || '';
    const prov = (S.provenance && S.provenance.source) || (st.provenance && st.provenance.source) || '';
    const fresh = S.lastRx && now - S.lastRx < STALE_MS;
    if (kind === 'sim' || prov === 'simulated') return ['simulated', 'Simulated · not the sleeve'];
    if (kind === 'replay' || prov === 'replay') return ['replay', 'Replay · recorded, not live'];
    if (kind === 'stream') return ['stream', 'Recorded stream · not live'];
    if (st.connection !== 'connected' || !fresh) return ['stale', 'No signal · sleeve not sending'];
    if ((kind === 'wifi' || kind === 'serial') && prov === 'live') return ['live', 'Live · from the sleeve'];
    return ['stale', 'Source unknown'];
  }

  // ---------------------------------------------------------------- render loop
  let lastFrame = performance.now();
  let lastText = 0;
  function frame() {
    const now = performance.now();   // not the rAF timestamp: it can be older than a message's performance.now()
    const dt = Math.min(0.1, (now - lastFrame) / 1000);
    lastFrame = now;
    const [src, label] = source(now);
    const live = src === 'live' || src === 'simulated' || src === 'replay' || src === 'stream';
    const stale = !S.lastRx || now - S.lastRx > STALE_MS;

    animate(S.grip, stale ? null : S.grip.target, dt, now, el.fillGrip, el.peakGrip, el.trackGrip);
    animate(S.vib, stale ? null : S.vib.target, dt, now, el.fillVib, el.peakVib, el.trackVib);

    if (now - lastText > 100) {              // text ~10x/s is plenty and keeps digits readable
      lastText = now;
      document.body.dataset.source = src;
      el.badge.textContent = label;

      const calibrated = S.calState === 'calibrated';
      el.ovGrip.hidden = calibrated || !live;
      if (S.capturing) el.ovGrip.innerHTML = S.capturing === 'relaxed' ? 'Relax your arm…' : 'Squeeze hard!';
      else if (MODE === 'phone') el.ovGrip.innerHTML = S.calState === 'failed' ? 'Calibration didn’t finish<small>run Setup again on the phone</small>' : 'Calibrate<small>run Setup on the phone</small>';
      else if (S.calState === 'relaxed_captured') el.ovGrip.innerHTML = 'Now squeeze<small>press S</small>';
      else if (S.calState === 'failed') el.ovGrip.innerHTML = 'Calibration didn’t finish<small>try again: R relax · S squeeze</small>';
      else el.ovGrip.innerHTML = 'Calibrate<small>R relax · S squeeze</small>';
      el.pctGrip.textContent = !stale && calibrated && S.grip.text != null ? String(S.grip.text) : '--';
      el.dGrip.textContent = calibrated ? 'of max squeeze' : 'not calibrated';

      el.pctVib.textContent = !stale && S.vib.target != null ? String(Math.round(S.vib.target * 100)) : '--';
      el.dVib.textContent = !stale && S.vib.ms2 != null ? `${S.vib.ms2.toFixed(2)} m/s²` : '-- m/s²';

      el.emgRate.textContent = rate(S.rate.emg, now);
      el.imuRate.textContent = rate(S.rate.imu, now);
      if (MODE === 'phone') el.gaps.textContent = S.phone && S.phone.device ? String(S.phone.lost) : '--';
      else el.gaps.textContent = S.next.boot ? String(S.gaps) : '--';
      el.age.textContent = S.lastRx ? fmtAge(now - S.lastRx) : '--';

      const canCal = S.wsOpen && S.status && S.status.connection === 'connected' && !stale;
      el.btnRelaxed.disabled = !canCal || !!S.capturing;
      el.btnSqueeze.disabled = !canCal || !!S.capturing || S.calState !== 'relaxed_captured';
    }
    requestAnimationFrame(frame);
  }

  function animate(m, target, dt, now, fill, peak, track) {
    const goal = target == null ? 0 : target;
    // fast attack, slower release, like a VU / rev meter
    const k = goal > m.shown ? 18 : 6;
    m.shown += (goal - m.shown) * (1 - Math.exp(-k * dt));
    if (m.shown > m.peak || now - m.peakAt > PEAK_HOLD_MS) {
      if (m.shown >= m.peak) { m.peak = m.shown; m.peakAt = now; }
      else m.peak = Math.max(m.shown, m.peak - 0.5 * dt);        // peak marker falls after the hold
    }
    fill.style.setProperty('--level', `${(m.shown * 100).toFixed(2)}%`);
    peak.style.setProperty('--peak', `calc(${(m.peak * 100).toFixed(2)}% - 0.35vh)`);
    peak.style.opacity = m.peak > 0.01 ? '0.9' : '0';
    track.setAttribute('aria-valuenow', String(Math.round(m.shown * 100)));
  }

  function rate(list, now) {
    while (list.length && now - list[0][0] > RATE_WINDOW_MS) list.shift();
    if (!list.length) return '--';
    const span = Math.max(500, now - list[0][0]);
    const n = list.reduce((a, b) => a + b[1], 0) - list[0][1];  // samples after the first batch's arrival
    return String(Math.round(n / (span / 1000)));
  }
  function fmtAge(ms) { return ms < 1000 ? `${Math.round(ms)} ms ago` : `${(ms / 1000).toFixed(1)} s ago`; }

  // ---------------------------------------------------------------- calibration
  let reqN = 0;
  async function calibrate(action) {
    try {
      const r = await fetch(`/api/runtimes/${encodeURIComponent(RUNTIME)}/calibration`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action, request_id: `tv-cal-${Date.now()}-${++reqN}` }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        toast(`Calibration refused: ${body.detail || r.status}`);
        return;
      }
      toast(action === 'relaxed' ? 'Relax your arm for 3 seconds…' : 'Squeeze as hard as you can for 3 seconds…', 3500);
    } catch (e) {
      toast('Could not reach the laptop.');
    }
  }
  el.btnRelaxed.addEventListener('click', () => calibrate('relaxed'));
  el.btnSqueeze.addEventListener('click', () => calibrate('squeeze'));
  document.addEventListener('keydown', (e) => {
    if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === 'r' && !el.btnRelaxed.disabled) calibrate('relaxed');
    else if (k === 's' && !el.btnSqueeze.disabled) calibrate('squeeze');
    else if (k === 'f') {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen && document.documentElement.requestFullscreen().catch(() => {});
    }
  });

  // ---------------------------------------------------------------- connection
  let backoff = 500;
  function connectPhone() {
    const es = new EventSource('/api/tv/events');      // reconnects by itself
    es.onopen = () => { S.wsOpen = true; el.toast.hidden = true; };
    es.onmessage = (ev) => { try { onPhone(JSON.parse(ev.data)); } catch (e) { /* ignore a bad event */ } };
    es.onerror = () => { S.wsOpen = false; };
  }

  function connect() {
    if (MODE === 'phone') { connectPhone(); return; }
    if (location.protocol === 'file:') {
      S.wsOpen = false;
      toast('Open this page from the laptop: http://127.0.0.1:8000/dashboard/tv.html', 0);
      return;
    }
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/live` +
      `?channels=telemetry,derived,device,runtime&runtime=${encodeURIComponent(RUNTIME)}`;
    let ws;
    try { ws = new WebSocket(url); } catch (e) { setTimeout(connect, backoff); return; }
    ws.onopen = () => { S.wsOpen = true; backoff = 500; el.toast.hidden = true; };
    ws.onmessage = (ev) => { try { onMessage(JSON.parse(ev.data)); } catch (e) { /* ignore a bad frame */ } };
    ws.onclose = () => {
      S.wsOpen = false;
      setTimeout(connect, backoff);
      backoff = Math.min(5000, backoff * 2);
    };
  }

  if (MODE === 'phone') {
    el.calBtns.hidden = true;                 // calibration is the phone's Setup; the TV only shows it
    el.gapsLabel.textContent = 'Missing records';
  }
  connect();
  requestAnimationFrame(frame);
})();
