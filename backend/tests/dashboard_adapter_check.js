// Run by test_dashboard_adapter.py: loads the dashboard's contract.js + backend-adapter.js with a fake
// browser (WebSocket, fetch) and checks what the adapter emits for backend messages.
const fs = require('fs'), vm = require('vm'), path = require('path');
const dash = path.join(__dirname, '..', '..', 'dashboard', 'js');
const sockets = [];
class FakeWS { constructor(u) { this.url = u; this.readyState = 1; sockets.push(this); setTimeout(() => this.onopen && this.onopen(), 0); } close() { this.onclose && this.onclose(); } }
const routes = {};
const fetchLog = [];
const ctx = { window: {}, WebSocket: FakeWS, location: { protocol: 'http:', host: 'h:1' }, performance: { now: () => Date.now() },
  setTimeout, clearTimeout, setInterval, clearInterval, console,
  fetch: async (url, o = {}) => { fetchLog.push([o.method || 'GET', url, o.body]); const r = routes[(o.method || 'GET') + ' ' + url.split('?')[0]];
    const v = typeof r === 'function' ? r(o) : r; if (!v) return { ok: false, status: 404, json: async () => ({ detail: 'nope' }) };
    return { ok: v.ok !== false, status: v.status || 200, json: async () => v.body }; } };
ctx.window = ctx; vm.createContext(ctx);
for (const f of ['contract.js', 'backend-station.js', 'backend-adapter.js']) vm.runInContext(fs.readFileSync(path.join(dash, f), 'utf8'), ctx, { filename: f });
const VG = ctx.VG, out = {};
const emgMsg = (over = {}) => ({ channel: 'derived', runtime: 'wearable', payload: { type: 'stream_features', stream: 'emg', device_id: 'd1', source_seq: 7, t_us: 5000000,
  provenance: { source: 'simulated' }, quality: { clipped_samples: 0 },
  features: { emg_activation: { envelope: { value: { emg0: 183.1 }, units: { emg0: 'x' } }, normalized: { status: 'ok', display: { emg0: 0.5 } }, calibration: { state: 'calibrated' } } }, ...over } });
const status = (o = {}) => ({ runtime: 'wearable', device_id: 'd1', state: 'ready', connection: 'connected', calibration: { state: 'calibrated' }, active_session_id: null, ...o });

(async () => {
  routes['GET /api/runtimes/wearable'] = { body: status() };
  routes['GET /api/station/sessions/ss0'] = { body: { kind: 'station', station_session_id: 'ss0', state: 'completed', source: 'simulated', protocol: { mode: 'preview' }, started_at: '2026-11-07T21:00:00Z', ended_at: '2026-11-07T21:00:30Z',
      trials: [{ trial_id: 'a', stimulus: { motor_setting: 150 }, response: 'felt', is_catch: false, response_latency_ms: 300 }, { trial_id: 'b', stimulus: { motor_setting: 110 }, response: 'not_felt', is_catch: false, response_latency_ms: 410.4 }, { trial_id: 'c', stimulus: { motor_setting: 0 }, response: 'missing', is_catch: true, response_latency_ms: null }] } };
  routes['GET /api/history'] = { body: { items: [
    { kind: 'wearable', session_id: 's1', device_id: 'd1', state: 'completed', end_reason: 'stopped_by_user', source: 'replay', started_at: '2026-11-07T20:00:00Z', ended_at: '2026-11-07T20:00:30Z', calibration_id: 'c1', score: { at_start: { cumulative_au: 1 }, at_end: { cumulative_au: 3.5 } } },
    { kind: 'station', station_session_id: 'ss0', state: 'completed', trial_count: 3 },
    { kind: 'station', station_session_id: 'ss00', state: 'testing' }, { kind: 'wearable', session_id: 's2', state: 'recording' }] } };
  const a = new VG.BackendAdapter({});
  const ev = { telemetry: [], state: [], connection: [], session: [], history: [], calibration: [] };
  for (const k of Object.keys(ev)) a.on(k, (m) => ev[k].push(m));
  out.wsUrl = a.wsUrl;
  a.connect(); await new Promise((r) => setTimeout(r, 300));
  const ws = sockets[0];
  ws.onmessage({ data: JSON.stringify({ channel: 'snapshot', payload: { runtimes: [status({ latest: {} })] } }) });
  out.state = ev.state.map((s) => s.device_role + ':' + s.state);
  out.histStation = ev.history[0].tests.map((t) => [t.test_id, t.provenance, t.threshold_setting, t.trials.length, t.trials[1].rt_ms, t.catch_trials, t.missing_responses]);
  out.history = ev.history[0] && ev.history[0].sessions.map((s) => [s.session_id, s.provenance, s.duration_s, s.score_total, s.ended_reason]);

  // IMU: only a converted (physical) value is shown; EMG 0..1 display becomes a percentage
  ws.onmessage({ data: JSON.stringify({ channel: 'derived', runtime: 'wearable', payload: { type: 'stream_features', stream: 'imu', features: { vibration: { physical: { status: 'ok', combined_rms: 2.5 } } } } }) });
  ws.onmessage({ data: JSON.stringify({ channel: 'derived', runtime: 'wearable', payload: { type: 'experimental_score', status: 'accumulating', rate: { value: 4.2, units: 'au/h' }, cumulative: { value: 10 } } }) });
  ws.onmessage({ data: JSON.stringify(emgMsg()) });
  let t = ev.telemetry.at(-1);
  out.t1 = [t.values.activation_pct, t.values.vib_ahv_est, t.values.score_rate, t.values.score_total, t.provenance, t.quality.calibrated, t.values.emg_env];
  // not calibrated: no activation; unconverted IMU: no vibration; paused score: no rate
  ws.onmessage({ data: JSON.stringify({ channel: 'derived', runtime: 'wearable', payload: { type: 'stream_features', stream: 'imu', features: { vibration: { physical: { status: 'warming_up', combined_rms: null } } } } }) });
  ws.onmessage({ data: JSON.stringify({ channel: 'derived', runtime: 'wearable', payload: { type: 'experimental_score', status: 'paused', rate: { value: null }, cumulative: { value: 10 } } }) });
  ws.onmessage({ data: JSON.stringify(emgMsg({ features: { emg_activation: { envelope: { value: { emg0: 9 } }, normalized: { status: 'unavailable', display: null }, calibration: { state: 'invalidated' } } } })) });
  t = ev.telemetry.at(-1);
  out.t2 = [t.values.activation_pct, t.values.vib_ahv_est, t.values.score_rate, t.quality.calibrated];
  // another runtime's data is ignored
  const n = ev.telemetry.length; ws.onmessage({ data: JSON.stringify({ ...emgMsg(), runtime: 'replay-x' }) }); out.ignoredOther = ev.telemetry.length === n;
  // session total = cumulative - value at the start of the recording
  routes['POST /api/sessions'] = { body: { session_id: 's9', score: { at_start: { cumulative_au: 4 } } } };
  const st = await a.send('start_session'); out.start = [st.status, st.session_id];
  ws.onmessage({ data: JSON.stringify({ channel: 'derived', runtime: 'wearable', payload: { type: 'experimental_score', status: 'accumulating', rate: { value: 1 }, cumulative: { value: 4.75 } } }) });
  ws.onmessage({ data: JSON.stringify(emgMsg()) });
  out.total = ev.telemetry.at(-1).values.score_total;
  routes['POST /api/sessions/s9/stop'] = { body: { session_id: 's9', state: 'completed', end_reason: 'stopped_by_user', source: 'simulated', started_at: '2026-11-07T20:00:00Z', ended_at: '2026-11-07T20:00:10Z', calibration_id: null, counters: { readings: { mean_activation_pct: 42.5, peak_vibration_ms2: 3.1 } } } };
  await a.send('stop_session');
  out.session = ev.session.map((s) => [s.session_id, s.ended_reason, s.score_total, s.duration_s, s.mean_activation_pct, s.peak_vib_ahv_est]);
  // a recording the backend already ended (the replay reached its end): Stop is not an error
  routes['POST /api/sessions'] = { body: { session_id: 's10', score: { at_start: { cumulative_au: 0 } } } };
  await a.send('start_session');
  routes['POST /api/sessions/s10/stop'] = { ok: false, status: 409, body: { detail: 'not recording' } };
  routes['GET /api/sessions/s10'] = { body: { session_id: 's10', state: 'completed', end_reason: 'end_of_input', source: 'replay', started_at: '2026-11-07T20:00:00Z', ended_at: '2026-11-07T20:00:04Z' } };
  const late = await a.send('stop_session');
  out.lateStop = [late.status, late.already_ended, ev.session.at(-1).ended_reason];
  // perception test on the backend's per-trial API: a short preview with scripted answers
  const sev = [], calls = [];
  const b = new VG.BackendAdapter({ stationGapMs: [5, 10] });
  b.on('station', (m) => sev.push(m)); b.on('state', (m) => sev.push(['state', m.device_role, m.state]));
  b.connect(); await new Promise((r) => setTimeout(r, 50));
  b.isOpen = true;
  let cnt = 0; const answers = ['felt', 'felt', 'not_felt', null /* catch trial 4: nothing felt */, 'felt', 'not_felt'];
  routes['POST /api/station/sessions'] = (o) => { calls.push(['start', JSON.parse(o.body)]); return { body: { station_session_id: 'ss1' } }; };
  routes['POST /api/station/sessions/ss1/stimulus'] = (o) => { const body = JSON.parse(o.body); calls.push(["stimulus", body]); cnt++;
    setTimeout(() => { const ans = answers[cnt - 1]; const k = cnt; if (ans) b.send("submit_response", { trial_id: "tr" + k, response: ans }); }, 5);
    return { body: { trial_id: "tr" + cnt } }; };
  routes['POST /api/station/sessions/ss1/respond'] = (o) => { const body = JSON.parse(o.body); return { body: { response: body.response, response_latency_ms: 123.4 } }; };
  routes['GET /api/station/sessions/ss1'] = { body: { trials: [0, 1, 2, 3, 4, 5].map((i) => ({ response: i === 3 ? 'missing' : 'felt', response_latency_ms: null })) } };
  routes['POST /api/station/sessions/ss1/complete'] = { body: { state: 'completed' } };
  const started = await b.send('start_station_test', { mode: 'preview' });
  out.stStart = started.status;
  for (let i = 0; i < 100 && !sev.some((m) => m.type === 'station_complete'); i++) await new Promise((r) => setTimeout(r, 50));
  const done = sev.find((m) => m.type === 'station_complete');
  out.stSource = calls[0][1].source;                                   // never labelled live
  out.stStim = calls.filter((c) => c[0] === 'stimulus').map((c) => [c[1].stimulus.motor_setting, c[1].is_catch]);
  out.stResults = sev.filter((m) => m.type === 'station_trial_result').map((m) => [m.index, m.response, m.stimulus_setting]);
  out.stDone = done && [done.provenance, done.threshold_setting, done.reversals, done.catch_trials, done.catch_trials_felt, done.missing_responses, done.measured_amplitude_available];
  out.stStates = sev.filter((m) => Array.isArray(m) && m[1] === 'station').map((m) => m[2]);
  // a second start while one runs is refused; abort with nothing running is an error
  out.stAbortIdle = (await b.send('abort_station_test')).status;
  b.disconnect();
  // runtime error / disconnected states
  ws.onmessage({ data: JSON.stringify({ channel: 'runtime', runtime: 'wearable', payload: status({ connection: 'disconnected', connection_detail: 'unplugged' }) }) });
  out.disc = ev.state.at(-1).state + ':' + ev.state.at(-1).detail;
  ws.onmessage({ data: JSON.stringify({ channel: 'runtime', runtime: 'wearable', payload: status({ error: 'boom' }) }) });
  out.err = ev.state.at(-1).state + ':' + ev.state.at(-1).detail;
  // calibration refused by the backend is reported as failed with the backend's reason
  routes['POST /api/runtimes/wearable/calibration'] = { ok: false, status: 409, body: { detail: 'recording in progress' } };
  await a.send('calibrate'); await new Promise((r) => setTimeout(r, 200));
  out.cal = ev.calibration.map((c) => c.phase + ':' + c.reason);
  a.disconnect();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
