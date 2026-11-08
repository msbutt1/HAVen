/*
 * VibeGuard dashboard app. Talks only to the adapter interface, so swapping
 * mock data for the real backend is a URL change: ?source=ws&url=ws://...
 */
(function () {
  'use strict';
  const VG = window.VG;
  const $ = (id) => document.getElementById(id);

  const params = new URLSearchParams(location.search);
  // Served by the backend (http/https, not a file) -> the real backend; opened as a file -> mock data.
  // ?source=mock | backend | ws (the old placeholder WebSocket) overrides.
  const SOURCE = ['mock', 'backend', 'ws'].includes(params.get('source'))
    ? params.get('source')
    : (location.protocol === 'file:' ? 'mock' : 'backend');
  const adapter = SOURCE === 'ws'
    ? new VG.WebSocketAdapter(params.get('url') || VG.contract.DEFAULT_WS_URL)
    : SOURCE === 'backend'
      ? new VG.BackendAdapter({ base: params.get('base') || '', runtime: params.get('runtime') || 'wearable' })
      : new VG.MockAdapter();

  const STALE_MS = 1000;
  const STATE_LABEL = {
    disconnected: 'Disconnected', ready: 'Ready', calibrating: 'Calibrating', recording: 'Recording',
    testing: 'Testing', completed: 'Completed', error: 'Error',
  };
  const RESPONSE_LABEL = { felt: 'Felt it', not_felt: 'Didn’t feel it', missing: 'No response' };

  const S = {
    view: 'live',
    connected: false,
    devices: { sleeve: { state: 'disconnected', detail: '' }, station: { state: 'disconnected', detail: '' } },
    lastTelemetryAt: 0,
    latest: null,
    calibration: null,
    cal: { phase: null, remaining: 0, phaseMs: 3000, msg: '' },
    session: null,
    busy: false,
    saved: [],
    tool: null,
    events: [],
    recalibrate: false,
    st: { view: 'idle', mode: 'preview', planned: null, trials: [], current: null, result: null, msg: '' },
  };

  const charts = {
    act: new VG.LineChart($('chart-act'), { min: 0, max: 100, colorVar: '--c-act' }),
    vib: new VG.LineChart($('chart-vib'), { min: 0, floorMax: 1, colorVar: '--c-vib' }),
    score: new VG.LineChart($('chart-score'), { min: 0, floorMax: 1, colorVar: '--c-score', dashed: true }),
    raw: new VG.LineChart($('chart-raw'), { min: 0, floorMax: 200, windowMs: 15000, colorVar: '--c-act' }),
  };

  // ---------- helpers ----------
  const fmtNum = (v, dp) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(dp));
  // small experimental-score values (arbitrary units) need more digits than "0.0" to be readable
  const fmtScore = (v) => (v == null || Number.isNaN(v) ? '—' : Math.abs(v) < 1 ? Number(v).toFixed(3) : Number(v).toFixed(1));
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—');
  const fmtDur = (ms) => {
    const s = Math.max(0, Math.round(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  };
  let toastTimer;
  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
  }
  function freshness(now) {
    if (!S.connected || !S.latest) return 'none';
    return now - S.lastTelemetryAt > STALE_MS ? 'stale' : 'fresh';
  }

  // ---------- views / tabs ----------
  const VIEWS = ['live', 'calibrate', 'station', 'history'];
  function showView(v) {
    if (!VIEWS.includes(v)) v = 'live';
    S.view = v;
    for (const name of VIEWS) {
      $(`view-${name}`).hidden = name !== v;
      const tab = $(`tab-${name}`);
      tab.setAttribute('aria-selected', String(name === v));
      tab.tabIndex = name === v ? 0 : -1;
    }
    if (location.hash !== `#${v}`) history.replaceState(null, '', `#${v}`);
    if (v === 'history') drawHistoryCharts();
  }
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.addEventListener('click', () => showView(b.dataset.view));
    b.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const i = VIEWS.indexOf(S.view) + (e.key === 'ArrowRight' ? 1 : -1);
      const next = VIEWS[(i + VIEWS.length) % VIEWS.length];
      showView(next);
      $(`tab-${next}`).focus();
    });
  });
  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));

  // ---------- connection + device state ----------
  function renderDevices() {
    for (const role of ['sleeve', 'station']) {
      const d = S.devices[role];
      const el = $(`dev-${role}`);
      el.dataset.state = d.state;
      el.querySelector('.device-state').textContent = STATE_LABEL[d.state] || d.state;
      el.title = d.detail || '';
    }
    $('btn-connect').textContent = S.connected ? 'Disconnect' : 'Connect';

    const errs = ['sleeve', 'station'].filter((r) => S.devices[r].state === 'error');
    const banner = $('error-banner');
    banner.hidden = !errs.length;
    banner.textContent = errs.map((r) => `${r === 'sleeve' ? 'Sleeve' : 'Station'} error: ${S.devices[r].detail || 'unknown problem'}`).join(' ');
  }

  function renderSource() {
    const b = $('source-band');
    let src = 'none';
    let text = 'Not connected. Press Connect to start the data feed.';
    if (S.connected && S.latest) {
      src = S.latest.provenance;
      if (src === 'live') text = `Live data from ${S.latest.device_id}`;
      else if (src === 'replay') text = `Recorded replay${S.latest.recording_id ? ` of ${S.latest.recording_id}` : ''}. This is not live.`;
      else text = `Simulated data${SOURCE === 'mock' ? ' from the mock feed' : ''}. These are not real measurements.`;
    } else if (S.connected) {
      text = 'Connected. Waiting for the first reading.';
    }
    b.dataset.source = src;
    b.textContent = text;
  }

  $('btn-connect').addEventListener('click', () => (S.connected ? adapter.disconnect() : adapter.connect()));

  adapter.on('connection', (m) => {
    S.connected = m.connected;
    if (!m.connected) {
      S.latest = null;
      const now = performance.now();
      for (const c of Object.values(charts)) c.push(now, null); // break the line across the gap
      S.devices.sleeve = { state: 'disconnected', detail: m.reason || '' };
      S.devices.station = { state: 'disconnected', detail: m.reason || '' };
      S.session = null;
      if (S.st.view === 'running') {
        S.st.view = 'idle';
        S.st.current = null;
        S.st.msg = `${m.reason || 'Connection lost'}. The test was stopped and nothing was saved.`;
      }
      if (S.cal.phase === 'relax' || S.cal.phase === 'squeeze') {
        S.cal.phase = 'failed';
        S.cal.msg = `${m.reason || 'Connection lost'}. Calibration was not saved.`;
      }
      if (m.reason && m.reason !== 'Disconnected by user') toast(m.reason);
    }
    renderAll();
  });

  adapter.on('state', (m) => {
    S.devices[m.device_role] = { state: m.state, detail: m.detail || '' };
    if (m.device_role === 'sleeve' && m.state !== 'recording') S.session = null;
    renderAll();
  });

  // ---------- live ----------
  adapter.on('telemetry', (m) => {
    const now = performance.now();
    if (S.lastTelemetryAt && now - S.lastTelemetryAt > STALE_MS) {
      for (const c of Object.values(charts)) c.push(now - 1, null); // gap after stale data
    }
    S.lastTelemetryAt = now;
    const first = !S.latest;
    S.latest = m;
    const v = m.values;
    charts.act.push(now, v.activation_pct);
    charts.vib.push(now, v.vib_ahv_est);
    charts.score.push(now, v.score_rate);
    charts.raw.push(now, v.emg_env);
    if (m.units) {
      const u = { 'pts/min': 'points / min', pts: 'points' };
      if (m.units.score_rate) $('score-rate-unit').textContent = u[m.units.score_rate] || m.units.score_rate;
      if (m.units.score_total) $('score-total-unit').textContent = u[m.units.score_total] || m.units.score_total;
    }
    renderReadouts();
    if (first) renderSource();
  });

  function renderReadouts() {
    const now = performance.now();
    const f = freshness(now);
    const v = f === 'fresh' ? S.latest.values : null;
    document.querySelectorAll('.readout').forEach((el) => el.classList.toggle('is-stale', f !== 'fresh'));

    // unbounded above (may exceed 100 %); below the relaxed baseline it reads 0 and the note says so
    $('act-num').textContent = fmtNum(v && v.activation_pct != null ? Math.max(0, v.activation_pct) : null, 0);
    $('vib-num').textContent = fmtNum(v && v.vib_ahv_est, 2);
    $('score-rate').textContent = fmtNum(v && v.score_rate, 2);
    $('score-total').textContent = fmtScore(v && v.score_total);
    const bar = v && (v.activation_bar_pct != null ? v.activation_bar_pct : v.activation_pct);
    $('act-fill').style.width = bar != null ? `${Math.max(0, Math.min(100, bar))}%` : '0%';

    const fr = $('freshness');
    fr.dataset.fresh = f;
    if (f === 'none') fr.textContent = S.connected ? 'Connected. Waiting for the first reading.' : 'No data. Connect the sleeve.';
    else if (f === 'stale') fr.textContent = `No new reading for ${((now - S.lastTelemetryAt) / 1000).toFixed(1)} s. Values are hidden until data resumes.`;
    else fr.textContent = `Updating ${S.latest.sample_rate_hz} times a second.`;

    const calibrated = S.calibration || (S.latest && S.latest.quality && S.latest.quality.calibrated);
    const ql = (v && S.latest.quality) || {};
    const flags = [];
    if (ql.above_max) flags.push('Above your calibrated maximum.');
    if (ql.below_rest) flags.push('Below your relaxed baseline.');
    if (ql.degraded) flags.push('Some samples clipped: treat with caution.');
    $('act-note').textContent = !calibrated
      ? (S.recalibrate ? 'The wearable restarted, so the calibration was dropped. Calibrate again.' : 'Not calibrated. Run calibration to see grip effort.')
      : [S.calibration ? `Calibrated at ${fmtTime(S.calibration.at)}.` : 'Calibrated before this page opened.', ...flags].join(' ')
        + ' Muscle activity, not grip force.';

    const q = $('quality');
    const artifact = v && S.latest.quality && S.latest.quality.emg_vibration_artifact;
    q.hidden = !artifact;
    q.textContent = artifact ? 'Tool vibration may be leaking into the muscle signal. Treat activation and score with caution.' : '';
  }

  function renderRecording() {
    const st = S.devices.sleeve.state;
    const rec = st === 'recording';
    const btn = $('btn-record');
    btn.textContent = rec ? 'Stop recording' : 'Start recording';
    btn.classList.toggle('btn-primary', !rec);
    btn.classList.toggle('btn-stop', rec);
    btn.disabled = S.busy || !(st === 'ready' || rec);
    let msg;
    if (rec) msg = S.session ? `Recording ${S.session.id}, ${fmtDur(Date.now() - S.session.startedAt)}` : 'Recording';
    else if (st === 'ready') msg = (S.calibration || (S.latest && S.latest.quality && S.latest.quality.calibrated)) ? 'Ready to record.' : 'Ready to record. Not calibrated, so the score will stay empty.';
    else if (st === 'calibrating') msg = 'Wait for calibration to finish.';
    else if (st === 'error') msg = 'Clear the sleeve error before recording.';
    else msg = 'Connect the sleeve to record.';
    $('rec-status').textContent = msg;
  }

  $('btn-record').addEventListener('click', async () => {
    const rec = S.devices.sleeve.state === 'recording';
    S.busy = true;
    renderRecording();
    const r = await adapter.send(rec ? 'stop_session' : 'start_session');
    S.busy = false;
    if (r.status === 'error') toast(r.error);
    else if (!rec) S.session = { id: r.session_id, startedAt: Date.now() };
    renderRecording();
  });

  // Recordings the backend already holds (shown without a "saved" toast).
  adapter.on('history', (m) => {
    const known = new Set(S.saved.map((i) => i.rec.session_id || i.rec.test_id));
    for (const rec of m.sessions) {
      if (!known.has(rec.session_id)) S.saved.push({ kind: 'session', at: rec.ended_at, rec });
    }
    for (const rec of m.tests || []) {
      if (!known.has(rec.test_id)) S.saved.push({ kind: 'station', at: rec.completed_at, rec });
    }
    S.saved.sort((a, b) => String(b.at).localeCompare(String(a.at))); // newest first
    renderHistory();
  });

  adapter.on('session', (m) => {
    S.saved.unshift({ kind: 'session', at: m.ended_at, rec: m });
    S.session = null;
    toast(m.ended_reason === 'stopped' ? 'Recording saved' : `Recording ended early and was saved: ${m.ended_reason}`);
    renderHistory();
    renderRecording();
  });

  // ---------- calibrate (two steps, as the processor's state machine: relaxed, then squeeze) ----------
  async function calStep(command) {
    S.cal.msg = '';
    if (command === 'calibrate_relaxed') charts.raw.clear();
    const r = await adapter.send(command);
    if (r.status === 'error') {
      S.cal.phase = 'failed';
      S.cal.msg = r.error;
      renderCal();
    }
  }
  $('btn-cal-relaxed').addEventListener('click', () => calStep('calibrate_relaxed'));
  $('btn-cal-squeeze').addEventListener('click', () => calStep('calibrate_squeeze'));
  $('btn-cal-cancel').addEventListener('click', () => calStep('calibrate_cancel'));

  adapter.on('calibration', (m) => {
    if (m.phase === 'done') {
      S.calibration = { rest: m.result.rest_env, max: m.result.max_env, at: m.result.calibrated_at };
      S.cal.phase = 'done';
      S.recalibrate = false;
      toast('Calibration saved');
    } else if (m.phase === 'failed') {
      S.cal.phase = 'failed';
      S.cal.msg = m.reason;
    } else if (m.phase === 'cancelled') {
      S.cal.phase = null;
      S.cal.msg = '';
    } else {
      S.cal.phase = m.phase;   // relax | relaxed_captured | squeeze
      S.cal.remaining = m.remaining_ms || 0;
      S.cal.phaseMs = m.phase_ms || 3500;
    }
    renderCal();
  });

  function renderCal() {
    const p = S.cal.phase;
    const step = p === 'relax' ? 0 : (p === 'relaxed_captured' || p === 'squeeze') ? 1 : p === 'done' ? 2 : -1;
    document.querySelectorAll('#cal-steps li').forEach((li, i) => {
      li.classList.toggle('is-active', i === step && p !== 'done');
      li.classList.toggle('is-done', step > i || (p === 'done' && i === 2));
    });
    const prompts = {
      relax: 'Relax your forearm completely, resting on a table. Hold still.',
      relaxed_captured: 'Relaxed level captured. Now squeeze as hard as you can and hold, then press Start squeeze.',
      squeeze: 'Squeeze as hard as you can and hold it.',
      done: 'Calibrated. Grip effort now shows as a percentage of that squeeze.',
      failed: S.cal.msg || 'Calibration failed. Start again with the relaxed step.',
    };
    $('cal-prompt').textContent = prompts[p]
      || (S.recalibrate ? 'The wearable restarted, so the calibration was dropped. Rest your arm on the table, then start again.'
        : 'Put the sleeve on, rest your arm on the table, then press Start relaxed.');
    const running = p === 'relax' || p === 'squeeze';
    $('cal-count').textContent = running ? Math.ceil(S.cal.remaining / 1000) : '';
    $('cal-bar').style.width = running ? `${(1 - S.cal.remaining / S.cal.phaseMs) * 100}%` : p === 'done' ? '100%' : '0%';

    const st = S.devices.sleeve.state;
    const ready = st === 'ready';
    const relaxedBtn = $('btn-cal-relaxed');
    const squeezeBtn = $('btn-cal-squeeze');
    relaxedBtn.hidden = p === 'relaxed_captured' || running;
    relaxedBtn.textContent = p === 'done' || S.calibration ? 'Recalibrate' : p === 'failed' ? 'Start relaxed again' : 'Start relaxed';
    relaxedBtn.disabled = !ready;
    squeezeBtn.hidden = p !== 'relaxed_captured';
    squeezeBtn.disabled = !(ready || st === 'calibrating');
    $('btn-cal-cancel').hidden = !(running || p === 'relaxed_captured');
    let hint = '';
    if (st === 'disconnected') hint = 'Connect the sleeve first.';
    else if (st === 'recording') hint = 'Stop recording before calibrating.';
    else if (st === 'error') hint = 'Clear the sleeve error first.';
    else if (SOURCE === 'mock' && p === 'relaxed_captured') hint = 'The mock feed squeezes by itself during this step.';
    $('cal-hint').textContent = hint;

    $('cal-rest').textContent = S.calibration ? fmtNum(S.calibration.rest, 0) : '—';
    $('cal-max').textContent = S.calibration ? fmtNum(S.calibration.max, 0) : '—';
    $('cal-at').textContent = S.calibration ? fmtTime(S.calibration.at) : 'Not yet';
  }

  // ---------- tool use (estimated from vibration) and events ----------
  const TOOL_STATE = { tool: 'Tool in use', no_tool: 'No tool', unknown: '—' };
  const WARNING_TEXT = {
    continuous_tool_use_long: 'Long continuous tool use. Consider a break.',
    session_tool_time_high: 'Tool time this session is high.',
    session_tool_time_approaching: 'Tool time this session is getting high.',
  };
  const fmtSecs = (s) => (s == null ? '—' : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, '0')} s`);

  adapter.on('tool', (m) => {
    S.tool = m;
    renderTool();
  });

  function renderTool() {
    const t = S.tool;
    const live = t && !t.saved && S.connected;
    $('tool-state').textContent = live && t.status === 'ok' ? (TOOL_STATE[t.state] || '—') : '—';
    $('tool-time').textContent = t ? fmtSecs(t.session_tool_s) + (t.saved ? ' (last known)' : '') : '—';
    $('tool-bouts').textContent = t && t.bouts != null ? String(t.bouts) : '—';
    $('tool-longest').textContent = t ? fmtSecs(t.longest_bout_s) : '—';
    $('tool-hz').textContent = live && t.dominant_hz != null ? `${Math.round(t.dominant_hz)} Hz` : '—';
    const w = $('tool-warnings');
    w.textContent = '';
    for (const k of (live && t.warnings) || []) {
      const li = document.createElement('li');
      li.textContent = WARNING_TEXT[k] || k.replace(/_/g, ' ');
      w.appendChild(li);
    }
    w.hidden = !w.children.length;
    $('tool-hint').textContent = t && t.saved
      ? `Last known before the backend restarted (saved ${fmtTime(t.saved_at)}).`
      : live && t.status !== 'ok' ? 'Waiting for enough vibration data.' : '';
    $('btn-new-shift').disabled = !S.connected || SOURCE === 'ws';
  }

  $('btn-new-shift').addEventListener('click', async () => {
    if (!confirm('Start a new shift? Tool time and bouts go back to zero.')) return;
    const r = await adapter.send('new_shift');
    toast(r.status === 'error' ? r.error : 'New shift started: tool time reset');
  });

  const EVENT_TEXT = {
    boot_change: 'The wearable restarted. Calibrate again.',
    seq_gap: 'Some records were lost on the way.',
    sample_gap: 'A gap in the samples.',
    stream_stale: 'No data from the wearable: values are hidden.',
    stream_recovered: 'Data resumed.',
    invalid_record: 'A record failed the checks and was rejected.',
    tool_session_reset: 'New shift: tool time reset.',
    rate_mismatch: 'The sample rate differs from what the wearable announced.',
  };
  function eventText(e) {
    if (e.event === 'calibration_state') {
      const st = (e.detail && e.detail.state) || '';
      return st === 'failed' ? `Calibration failed: ${(e.detail.reason || '').replace(/_/g, ' ')}.`
        : `Calibration: ${st.replace(/_/g, ' ')}.`;
    }
    return EVENT_TEXT[e.event] || e.event.replace(/_/g, ' ');
  }
  adapter.on('event', (e) => {
    if (e.event === 'boot_change' && !(e.detail && e.detail.previous_boot_id == null)) {
      S.recalibrate = true;
      S.calibration = null;
    }
    S.events.unshift({ ...e, text: eventText(e), at: e.at || new Date().toISOString() });
    S.events.length = Math.min(S.events.length, 30);
    renderEvents();
    renderCal();
  });
  function renderEvents() {
    const ol = $('events');
    ol.textContent = '';
    if (!S.events.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = 'Nothing yet.';
      ol.appendChild(li);
      return;
    }
    for (const e of S.events.slice(0, 12)) {
      const li = document.createElement('li');
      const time = document.createElement('time');
      time.textContent = fmtTime(e.at);
      const span = document.createElement('span');
      span.textContent = e.text;
      if (['boot_change', 'stream_stale', 'seq_gap', 'invalid_record'].includes(e.event)) span.className = 'warn';
      li.append(time, span);
      ol.appendChild(li);
    }
  }

  // ---------- station ----------
  $('btn-st-start').addEventListener('click', async () => {
    const mode = document.querySelector('input[name="st-mode"]:checked').value;
    S.st.msg = '';
    S.st.trials = [];
    S.st.result = null;
    const r = await adapter.send('start_station_test', { mode });
    if (r.status === 'error') S.st.msg = r.error;
    renderStation();
  });
  $('btn-st-abort').addEventListener('click', async () => {
    const r = await adapter.send('abort_station_test');
    if (r.status === 'error') toast(r.error);
  });
  $('btn-st-again').addEventListener('click', () => {
    S.st.view = 'idle';
    S.st.trials = [];
    S.st.result = null;
    renderStation();
  });
  $('btn-felt').addEventListener('click', () => respond('felt'));
  $('btn-notfelt').addEventListener('click', () => respond('not_felt'));

  async function respond(resp) {
    const c = S.st.current;
    if (!c || c.answered) return;
    c.answered = true;
    renderStation();
    const r = await adapter.send('submit_response', { trial_id: c.trial_id, response: resp });
    if (r.status === 'error') toast(r.error);
  }

  adapter.on('station', (m) => {
    const st = S.st;
    switch (m.type) {
      case 'station_started':
        Object.assign(st, { view: 'running', mode: m.mode, planned: m.planned_trials, trials: [], current: null, result: null, msg: '' });
        break;
      case 'station_trial':
        st.current = { ...m, receivedAt: performance.now(), answered: false };
        break;
      case 'station_trial_result':
        st.trials.push(m);
        if (st.current && st.current.trial_id === m.trial_id) st.current = null;
        break;
      case 'station_complete':
        st.view = 'complete';
        st.current = null;
        st.result = m;
        st.trials = m.trials && m.trials.length ? m.trials : st.trials;
        S.saved.unshift({ kind: 'station', at: m.completed_at, rec: m });
        renderHistory();
        toast('Perception test saved');
        break;
      case 'station_aborted':
        st.view = 'idle';
        st.current = null;
        st.msg = `Test stopped: ${m.reason}. ${SOURCE === 'backend' ? 'The backend keeps it as an aborted test; it is not a result and is not listed in History.' : 'Nothing was saved.'}`;
        break;
    }
    renderStation();
  });

  function renderStation() {
    const st = S.st;
    $('st-idle').hidden = st.view !== 'idle';
    $('st-running').hidden = st.view !== 'running';
    $('st-complete').hidden = st.view !== 'complete';

    const stationState = S.devices.station.state;
    $('btn-st-start').disabled = stationState !== 'ready';
    $('st-hint').textContent = stationState === 'disconnected' ? 'Connect the station first.'
      : stationState === 'error' ? 'Clear the station error first.' : '';

    const msg = $('st-msg');
    msg.hidden = !st.msg;
    msg.textContent = st.msg;

    if (st.view === 'running') {
      const n = st.current ? st.current.index : st.trials.length;
      $('st-progress').textContent = n
        ? (st.planned ? `Trial ${n} of ${st.planned}` : `Trial ${n}`)
        : 'Starting';
      const c = st.current;
      $('st-prompt').textContent = !c ? 'Keep your fingertip on the motor. Next trial coming.'
        : c.answered ? 'Answer recorded.' : 'Did you feel a vibration?';
      const waiting = !!c && !c.answered;
      $('btn-felt').disabled = !waiting;
      $('btn-notfelt').disabled = !waiting;
    }

    if (st.view === 'complete' && st.result) {
      const r = st.result;
      $('st-threshold').textContent = fmtNum(r.threshold_setting, 0);
      const parts = [];
      parts.push(r.threshold_setting == null
        ? 'Not enough reversals to estimate a threshold. Run the full session for a threshold.'
        : r.reversals.length === 1 ? 'Based on a single reversal, so treat it as rough.' : `Average of the last ${r.reversals.length} reversal settings.`);
      if (r.catch_trials) parts.push(`Catch trials with no vibration: ${r.catch_trials_felt} of ${r.catch_trials} answered “felt it”.`);
      if (r.missing_responses) parts.push(`${r.missing_responses} missing response${r.missing_responses > 1 ? 's' : ''}.`);
      if (!r.measured_amplitude_available) parts.push('Motor setting only; vibration amplitude was not measured.');
      if (r.provenance === 'simulated') parts.push('Simulated station: no motor was driven.');
      $('st-result-note').textContent = parts.join(' ');
    }

    // trial log; catch trials stay hidden until the test is complete
    const reveal = st.view === 'complete';
    const body = $('st-log-rows');
    body.textContent = '';
    for (const t of st.trials) {
      const tr = document.createElement('tr');
      if (t.response === 'missing') tr.className = 'missing';
      const setting = t.is_catch ? (reveal ? '0 (catch trial)' : '—') : String(t.stimulus_setting);
      for (const text of [t.index, setting, RESPONSE_LABEL[t.response] || t.response, t.rt_ms == null ? '—' : `${t.rt_ms} ms`]) {
        const td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
    $('st-log').hidden = !st.trials.length;
  }

  function updateResponseWindow(now) {
    const c = S.st.current;
    const pct = c && !c.answered ? Math.max(0, 1 - (now - c.receivedAt) / c.response_window_ms) * 100 : 0;
    $('st-window').style.width = `${pct}%`;
  }

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input[type="text"], textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (S.view === 'station' && S.st.view === 'running' && (k === 'f' || k === 'n')) {
      e.preventDefault();
      respond(k === 'f' ? 'felt' : 'not_felt');
    }
    if (SOURCE === 'mock' && k === 's' && !e.repeat) setSqueeze(true);
  });
  document.addEventListener('keyup', (e) => {
    if (SOURCE === 'mock' && e.key.toLowerCase() === 's') setSqueeze(false);
  });

  // ---------- history ----------
  function sourceTag(p) {
    const span = document.createElement('span');
    span.className = `tag tag-${p === 'simulated' ? 'sim' : p}`;
    span.textContent = p === 'live' ? 'Live' : p === 'replay' ? 'Replay' : 'Simulated';
    return span;
  }

  function renderHistory() {
    $('hist-empty').hidden = S.saved.length > 0;
    $('hist-table').hidden = S.saved.length === 0;
    const body = $('hist-rows');
    body.textContent = '';
    for (const item of S.saved) {
      const r = item.rec;
      const tr = document.createElement('tr');
      const cells = [fmtTime(item.at), item.kind === 'session' ? 'Wearable recording' : 'Perception test', null, ''];
      if (item.kind === 'session') {
        const bits = [`${fmtDur(r.duration_s * 1000)} long`];
        bits.push(r.score_total == null ? 'no score (not calibrated)' : `score ${fmtScore(r.score_total)} ${r.score_units || 'points'} (experimental)`);
        if (r.mean_activation_pct != null) bits.push(`mean activation ${fmtNum(r.mean_activation_pct, 0)} %`);
        if (r.peak_vib_ahv_est != null) bits.push(`peak vibration ${fmtNum(r.peak_vib_ahv_est, 2)} m/s²`);
        if (r.ended_reason !== 'stopped') bits.push(`ended early: ${r.ended_reason}`);
        cells[3] = bits.join(', ');
      } else {
        const bits = [r.threshold_setting == null ? 'no threshold reached' : `threshold setting ${fmtNum(r.threshold_setting, 0)}`];
        bits.push(`${r.trials.length} trials`);
        if (r.missing_responses) bits.push(`${r.missing_responses} missing`);
        bits.push(r.mode === 'preview' ? 'short preview' : 'full session');
        cells[3] = bits.join(', ');
      }
      cells.forEach((c, i) => {
        const td = document.createElement('td');
        if (i === 2) td.appendChild(sourceTag(r.provenance));
        else td.textContent = c;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
  }

  // Deterministic simulated 12-week history.
  const SIM = (() => {
    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5);
    const weeks = Array.from({ length: 12 }, (_, i) => i + 1);
    const score = weeks.map((w) => Math.round(310 + w * 6 + rnd() * 60));
    const threshold = weeks.map((w) => Math.round((w <= 4 ? 60 : 60 + (w - 4) * 5.5) + rnd() * 8));
    const baseline = Math.round(threshold.slice(0, 3).reduce((a, b) => a + b, 0) / 3);
    const rule = Math.round(baseline * 1.25);
    let flagFrom = null;
    for (let i = 1; i < threshold.length; i++) {
      if (threshold[i] > rule && threshold[i - 1] > rule) { flagFrom = i; break; }
    }
    return { labels: weeks.map((w) => `W${w}`), score, threshold, baseline, rule, flagFrom };
  })();

  function drawHistoryCharts() {
    VG.drawWeekly($('chart-hist-score'), { kind: 'bars', values: SIM.score, labels: SIM.labels, colorVar: '--c-score' });
    VG.drawWeekly($('chart-hist-threshold'), {
      kind: 'line', values: SIM.threshold, labels: SIM.labels, colorVar: '--c-act', flagFrom: SIM.flagFrom,
      max: 160,
      refs: [
        { value: SIM.baseline, label: `Baseline (weeks 1–3): ${SIM.baseline}` },
        { value: SIM.rule, label: `Baseline + 25%: ${SIM.rule}`, colorVar: '--danger' },
      ],
    });
  }
  $('hist-flag').textContent = SIM.flagFrom == null
    ? 'No flag in this simulated history.'
    : `Flagged from week ${SIM.flagFrom + 1}: threshold above this worker’s baseline by more than 25% two weeks in a row. In real use this would suggest seeing an occupational health doctor. The 25% rule is a team-chosen illustration, not a clinical cutoff.`;

  // ---------- mock controls ----------
  function setSqueeze(on) {
    if (SOURCE !== 'mock') return;
    adapter.setSqueeze(on);
    $('mk-squeeze').classList.toggle('is-held', on);
  }
  if (SOURCE === 'mock') {
    $('mock-panel').hidden = false;
    const sq = $('mk-squeeze');
    sq.addEventListener('pointerdown', () => setSqueeze(true));
    for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) sq.addEventListener(ev, () => setSqueeze(false));
    $('mk-vibe').addEventListener('change', (e) => adapter.setVibration(e.target.checked));
    $('mk-drop').addEventListener('click', () => adapter.simulateDrop());
    $('mk-error').addEventListener('click', () => adapter.injectError());
  }

  // ---------- loops ----------
  function renderAll() {
    renderDevices();
    renderSource();
    renderReadouts();
    renderRecording();
    renderCal();
    renderStation();
    renderTool();
    renderEvents();
  }

  function frame() {
    const now = performance.now();
    const stale = freshness(now) !== 'fresh';
    for (const c of Object.values(charts)) c.stale = stale;
    if (S.view === 'live') { charts.act.draw(now); charts.vib.draw(now); charts.score.draw(now); }
    else if (S.view === 'calibrate') charts.raw.draw(now);
    else if (S.view === 'station') updateResponseWindow(now);
    requestAnimationFrame(frame);
  }

  setInterval(() => { renderReadouts(); renderRecording(); }, 250);
  window.addEventListener('resize', () => { if (S.view === 'history') drawHistoryCharts(); });

  showView(location.hash.slice(1) || 'live');
  renderAll();
  renderHistory();
  requestAnimationFrame(frame);
  adapter.connect();

  if (SOURCE === 'backend') { // the backend's score is in arbitrary units from the first frame, not points
    $('score-rate-unit').textContent = 'au/h';
    $('score-total-unit').textContent = 'au';
  }

  window.VG.app = { adapter, state: S }; // handy in the browser console
})();
