/*
 * BackendAdapter: the dashboard's connection to the real HAVen backend (backend/ in this repo).
 * Same interface as MockAdapter, so app.js does not know which one it is talking to.
 *
 *   live data   WebSocket  /ws/live      (derived features, runtime state, sessions)
 *   commands    HTTP       /api/...      (sessions, calibration, history)
 *
 * It translates the backend's records into the dashboard's channels (see contract.js) and never invents
 * a number: anything the backend does not provide (for example vibration before the IMU is converted to
 * m/s², or the activation before calibration) stays null and the screens show a dash.
 *
 * Open with  index.html?source=backend  (the default when the page is served by the backend itself).
 * Optional:  &base=http://host:port  to point at a backend on another origin, &runtime=wearable.
 *
 * The perception-test (station) screens run on the backend's per-trial station API through StationRunner
 * (backend-station.js). The backend's station controller is SIMULATED (no motor), so every station session
 * is stored and shown as simulated; the test sequence itself is a stand-in until Fahad's engine is wired in.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const CAPTURE_MS = 3500; // each capture: 3 s plus 0.5 s discarded at the start (CalibrationConfig defaults)
  const POLL_MS = 2000; // backstop for runtime status, in case a push is missed
  // plain words for each calibration failure reason (firmware/INTEGRATION_FOR_ABDULJABBAR.md, section 3)
  const MISMATCH = 'This calibration belongs to a different device or session. Recalibrate.';
  const REASON_TEXT = {
    insufficient_separation: 'The squeeze wasn’t clearly stronger than rest. Squeeze harder, or check the pads, and try again.',
    insufficient_data: 'Not enough signal during the capture. Check the pads and try again.',
    interrupted_boot_change: 'The wearable restarted during calibration. Start again.',
    interrupted_processing_change: 'The processing settings changed during calibration. Start again.',
    channel_mismatch: MISMATCH, device_mismatch: MISMATCH, boot_mismatch: MISMATCH, provenance_mismatch: MISMATCH,
    processing_config_mismatch: 'The calibration was made with different settings. Recalibrate.',
    calibration_unverified: 'The saved calibration is waiting for new data to confirm it is the same wearable.',
    clipping: 'The signal was clipping (too strong for the sensor). Check the pads and try again.',
    device_disconnected: 'The wearable disconnected during calibration. Start again.',
    boot_change: 'The wearable restarted. Calibrate again.',
  };
  const reasonText = (r) => REASON_TEXT[r] || (r ? String(r).replace(/_/g, ' ') : 'Calibration did not complete. Try again.');
  const END_TEXT = {
    stopped_by_user: 'stopped',
    simulated_history: 'stopped', // vg-backend seed-history: a whole simulated recording, not one cut short
    end_of_input: 'the replayed recording reached its end',
    device_changed: 'a different sleeve connected',
    runtime_stopped: 'the backend stopped',
    runtime_error: 'the backend hit an error',
    backend_restart: 'the backend restarted during it',
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const first = (obj) => {
    if (!obj || typeof obj !== 'object') return null;
    const k = Object.keys(obj)[0];
    return k === undefined ? null : num(obj[k]);
  };

  class BackendAdapter extends VG.Emitter {
    constructor(opts = {}) {
      super();
      this.kind = 'backend';
      this.stationGapMs = opts.stationGapMs || null;
      this.runtime = opts.runtime || 'wearable';
      this.base = (opts.base || '').replace(/\/+$/, '');
      // "Basic …" for a backend started with VG_BASIC_AUTH, when the caller is not a browser that handles the
      // password itself (the Expo phone app). Sent on every request and on the WebSocket handshake.
      this.authorization = opts.authorization || null;
      // set by an app (not a browser): sent as X-HAVen-Client, so a missing password gets a plain 401 instead of
      // a challenge (iOS would otherwise hold the request, waiting for a password prompt)
      this.client = opts.client || null;
      this.wsUrl = opts.wsUrl || this._defaultWsUrl();
      this.ws = null;
      this.wantOpen = false;
      this.isOpen = false;
      this.retryMs = 500;
      this.rid = 0;

      this.status = null; // last runtime status from the backend
      this.lastState = '';
      this.imu = null; // latest IMU stream_features and when it arrived (performance.now)
      this.score = null;
      this.scoreBase = null; // cumulative score when the current recording started
      this.sessionId = null;
      this.calibrating = false;
      this.rateEma = null;
      this.lastEmgAt = 0;
      this.announced = new Set(); // session ids already reported as saved
      this.station = new VG.StationRunner(this);
      this.stationState = '';
      this._timer = null;
      this._refreshQueued = false;
    }

    // ---------------------------------------------------------------- lifecycle

    _defaultWsUrl() {
      if (this.base) return this.base.replace(/^http/, 'ws') + '/ws/live';
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${proto}//${location.host}/ws/live`;
    }

    connect() {
      this.wantOpen = true;
      if (!this.ws) this._open();
      if (!this._timer) this._timer = setInterval(() => this._refresh(), POLL_MS);
    }

    disconnect() {
      this.wantOpen = false;
      clearInterval(this._timer);
      this._timer = null;
      if (this.ws) this.ws.close();
    }

    _open() {
      let ws;
      try {
        // React Native's WebSocket takes headers as a third argument; a browser never gets one (no authorization)
        const wsHeaders = Object.assign({}, this.authorization ? { Authorization: this.authorization } : {},
          this.client ? { 'X-HAVen-Client': this.client } : {});
        ws = Object.keys(wsHeaders).length ? new WebSocket(this.wsUrl, [], { headers: wsHeaders }) : new WebSocket(this.wsUrl);
      } catch (e) {
        this._retry();
        return;
      }
      this.ws = ws;
      ws.onopen = () => {
        this.retryMs = 500;
        this.isOpen = true;
        this.emit('connection', { type: 'connection', connected: true });
        this._refresh();
        this._loadHistory();
      };
      ws.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        this._route(m);
      };
      ws.onclose = () => {
        this.ws = null;
        const was = this.isOpen;
        this.isOpen = false;
        this.status = null;
        this.lastState = '';
        this.stationState = '';
        this.station.connectionLost();
        if (was) {
          this.emit('connection', {
            type: 'connection', connected: false,
            reason: this.wantOpen ? 'Lost connection to the backend' : 'Disconnected by user',
          });
        }
        if (this.wantOpen) this._retry();
      };
    }

    _retry() {
      setTimeout(() => { if (this.wantOpen && !this.ws) this._open(); }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 5000);
    }

    // ---------------------------------------------------------------- incoming

    _route(m) {
      if (!m || typeof m.channel !== 'string') return;
      if (m.channel === 'snapshot') {
        const rts = (m.payload && m.payload.runtimes) || [];
        const rt = rts.find((r) => r.runtime === this.runtime);
        if (rt) {
          const lat = rt.latest || {};
          if (lat.imu && lat.imu.features) this.imu = { rec: lat.imu, at: performance.now() };
          if (lat.score) this.score = lat.score;
          if (lat.exposure && lat.exposure.type === 'exposure') this.emit('exposure', lat.exposure);
          if (lat.tool_use && lat.tool_use.type === 'tool_use') this.emit('tool', { ...lat.tool_use, saved: false });
          else if (rt.tool_use_saved && rt.tool_use_saved.record) {
            this.emit('tool', { ...rt.tool_use_saved.record, saved: true, saved_at: rt.tool_use_saved.saved_at });
          }
          this._applyStatus(rt);
        } else {
          this._emitState('disconnected', `The backend has no runtime called "${this.runtime}".`);
        }
        return;
      }
      if (m.runtime !== this.runtime) return;
      const p = m.payload || {};
      switch (m.channel) {
        case 'derived': this._derived(p); break;
        case 'runtime':
          if (p.runtime === this.runtime && p.state) this._applyStatus(p); // a full status push
          else this._refresh();
          break;
        case 'session':
          if (p.session && p.session.state && p.session.state !== 'recording') this._sessionEnded(p.session);
          this._refresh();
          break;
        default: break; // device, station: nothing the screens need yet
      }
    }

    _derived(p) {
      if (p.type === 'tool_use') {
        this.emit('tool', { ...p, saved: false });
      } else if (p.type === 'stream_event') {
        this.emit('event', { event: p.event, stream: p.stream, detail: p.detail || {}, at: new Date().toISOString() });
      } else if (p.type === 'experimental_score') {
        this.score = p;
      } else if (p.type === 'exposure') {
        this.emit('exposure', p); // daily exposure estimate (exposure.js renders it)
      } else if (p.type === 'stream_features' && p.stream === 'imu') {
        this.imu = { rec: p, at: performance.now() };
      } else if (p.type === 'stream_features' && p.stream === 'emg') {
        this._telemetry(p);
      }
    }

    _telemetry(p) {
      const now = performance.now();
      if (this.lastEmgAt) {
        const dt = now - this.lastEmgAt;
        if (dt > 0) this.rateEma = this.rateEma == null ? 1000 / dt : this.rateEma * 0.9 + (1000 / dt) * 0.1;
      }
      this.lastEmgAt = now;

      const act = (p.features && p.features.emg_activation) || {};
      const norm = act.normalized || {};
      const env = act.envelope || {};
      const calState = (act.calibration && act.calibration.state) || (this.status && this.status.calibration && this.status.calibration.state);
      // grip effort: `value` is unbounded (may go above 100 %), `display` is clamped for bars; a value is shown
      // only while the status is ok or degraded (clipped samples), never a missing value as 0
      const usable = norm.status === 'ok' || norm.status === 'degraded';
      const val = usable ? first(norm.value) : null;
      const disp = usable ? first(norm.display) : null;
      const activation = val == null ? null : Math.round(val * 1000) / 10;
      const activationBar = disp == null ? null : Math.round(disp * 1000) / 10;

      // Vibration only when the backend could convert to m/s²; and only while the IMU window is recent.
      let vib = null;
      if (this.imu && now - this.imu.at < 2000) {
        const v = this.imu.rec.features && this.imu.rec.features.vibration;
        const phys = v && v.physical;
        if (phys && phys.status === 'ok') vib = num(phys.combined_rms);
      }

      let rate = null;
      let total = null;
      const sc = this.score;
      if (sc && sc.rate) rate = num(sc.rate.value); // null while paused (a gap, warming up, not calibrated)
      if (this.sessionId && sc && sc.cumulative && this.scoreBase != null) {
        const c = num(sc.cumulative.value);
        if (c != null) total = Math.max(0, c - this.scoreBase);
      }

      const clipped = (p.quality && p.quality.clipped_samples) || 0;
      const prov = p.provenance || {};
      this.emit('telemetry', {
        type: 'telemetry',
        device_id: p.device_id,
        device_role: 'sleeve',
        seq: p.source_seq,
        t_ms: Math.round((p.t_us || 0) / 1000),
        sample_rate_hz: this.rateEma ? Math.max(1, Math.round(this.rateEma)) : 10,
        provenance: ['live', 'replay', 'simulated'].includes(prov.source) ? prov.source : 'simulated',
        recording_id: prov.source === 'replay' ? prov.recording_id || null : null,
        values: {
          emg_env: num(first(env.value)),
          activation_pct: activation,
          activation_bar_pct: activationBar,
          vib_ahv_est: vib,
          score_rate: rate,
          score_total: total,
        },
        units: { emg_env: 'counts rms', activation_pct: '%', vib_ahv_est: 'm/s²', score_rate: 'au/h', score_total: 'au' },
        quality: {
          emg_vibration_artifact: false, imu_saturated: clipped > 0, calibrated: calState === 'calibrated',
          above_max: usable && !!Object.values(norm.above_reference || {})[0],
          below_rest: usable && !!Object.values(norm.below_relaxed || {})[0],
          degraded: norm.status === 'degraded',
        },
      });
    }

    // ---------------------------------------------------------------- runtime state

    _mapState(rt) {
      if (rt.error) return ['error', String(rt.error)];
      if (rt.connection && rt.connection !== 'connected') {
        return ['disconnected', rt.connection_detail ? String(rt.connection_detail) : 'The wearable is not connected to the backend.'];
      }
      const s = rt.state;
      if (s === 'recording' || s === 'calibrating' || s === 'completed' || s === 'error') return [s, ''];
      if (s === 'disconnected') return ['disconnected', ''];
      if (rt.active_session_id) return ['recording', ''];
      return ['ready', ''];
    }

    _applyStatus(rt) {
      this.status = rt;
      this.sessionId = rt.active_session_id || null;
      const [state, detail] = this._mapState(rt);
      this._emitState(state, detail);
      if (!this.stationState) this._setStation('ready');
    }

    _emitState(state, detail) {
      const key = `sleeve|${state}|${detail}`;
      if (key !== this.lastState) {
        this.lastState = key;
        this.emit('state', { type: 'state', device_id: this.status ? this.status.device_id : null, device_role: 'sleeve', state, detail });
      }
    }

    _setStation(state) {
      this.stationState = state;
      this.emit('state', {
        type: 'state', device_id: 'station-simulated', device_role: 'station', state,
        detail: 'Simulated station: no motor is driven and results are labelled simulated.',
      });
    }

    async _refresh() {
      if (this._refreshQueued || !this.isOpen) return;
      this._refreshQueued = true;
      await sleep(80); // coalesce bursts of pushes
      this._refreshQueued = false;
      const r = await this._http('GET', `/api/runtimes/${encodeURIComponent(this.runtime)}`);
      if (r.ok && r.data && this.isOpen) this._applyStatus(r.data);
    }

    // ---------------------------------------------------------------- sessions and history

    _sessionRecord(row) {
      const score = row.score || {};
      const readings = (row.counters && row.counters.readings) || {};
      const at = score.at_start && score.at_start.cumulative_au;
      const end = score.at_end && score.at_end.cumulative_au;
      const started = row.started_at ? Date.parse(row.started_at) : null;
      const ended = row.ended_at ? Date.parse(row.ended_at) : null;
      // The backend's own figure for the session (score.session_au: end minus start of one accumulation, counted
      // from zero when nothing was scored before; null with a reason when it can't be attributed). Rows saved
      // before it existed fall back to end minus start.
      let scoreTotal = null;
      if (row.calibration_id) {
        if (Object.prototype.hasOwnProperty.call(score, 'session_au')) scoreTotal = num(score.session_au) == null ? null : Math.max(0, num(score.session_au));
        else if (num(at) != null && num(end) != null) scoreTotal = Math.max(0, end - at);
      }
      return {
        type: 'session_saved',
        session_id: row.session_id,
        device_id: row.device_id,
        provenance: ['live', 'replay', 'simulated'].includes(row.source) ? row.source : 'simulated',
        started_at: row.started_at,
        ended_at: row.ended_at,
        duration_s: started != null && ended != null ? Math.max(0, (ended - started) / 1000) : 0,
        ended_reason: END_TEXT[row.end_reason] || String(row.end_reason || 'stopped').replace(/_/g, ' '),
        calibrated: !!row.calibration_id,
        score_total: scoreTotal,
        score_units: 'au', // arbitrary units of the experimental formula
        // what the screens showed during the recording (backend: counters.readings); null when never available
        peak_vib_ahv_est: readings.peak_vibration_ms2 == null ? null : num(readings.peak_vibration_ms2),
        mean_activation_pct: readings.mean_activation_pct == null ? null : num(readings.mean_activation_pct),
        peak_activation_pct: readings.peak_activation_pct == null ? null : num(readings.peak_activation_pct),
        // estimated from vibration by the processor's tool-use detector; null when it never ran
        tool_time_s: readings.tool_time_s == null ? null : num(readings.tool_time_s),
        tool_bouts: readings.tool_bouts == null ? null : num(readings.tool_bouts),
      };
    }

    _stationRecord(row) {
      const mode = (row.protocol && row.protocol.mode) === 'full' ? 'full' : 'preview';
      const trials = (row.trials || []).map((t, i) => ({
        type: 'station_trial_result', test_id: row.station_session_id, trial_id: t.trial_id, index: i + 1,
        stimulus_setting: t.is_catch ? 0 : (t.stimulus && t.stimulus.motor_setting),
        measured_amplitude: t.measured_amplitude || null, is_catch: !!t.is_catch, response: t.response,
        rt_ms: t.response_latency_ms == null ? null : Math.round(t.response_latency_ms),
      }));
      return {
        type: 'station_complete', test_id: row.station_session_id, device_id: 'station-simulated',
        provenance: ['live', 'replay', 'simulated'].includes(row.source) ? row.source : 'simulated', mode,
        started_at: row.started_at, completed_at: row.ended_at, measured_amplitude_available: false, trials,
        ...VG.stationSummarize(trials, mode),
      };
    }

    _sessionEnded(row) {
      if (!row || !row.session_id || this.announced.has(row.session_id)) return;
      this.announced.add(row.session_id);
      this.emit('session', this._sessionRecord(row));
    }

    async _loadHistory() {
      const r = await this._http('GET', '/api/history?limit=200');
      if (!r.ok || !r.data || !Array.isArray(r.data.items)) return;
      const rows = r.data.items.filter((i) => i.kind === 'wearable' && i.state !== 'recording');
      rows.forEach((row) => this.announced.add(row.session_id));
      // newest first, as the History screen lists them
      // history rows carry only a summary: fetch the trials of the newest finished tests (the table needs them)
      const finished = r.data.items.filter((i) => i.kind === 'station' && i.state === 'completed').slice(0, 20);
      const details = await Promise.all(finished.map((row) =>
        this._http('GET', `/api/station/sessions/${encodeURIComponent(row.station_session_id)}`)));
      const tests = details.map((d, i) => this._stationRecord(d.ok && d.data ? d.data : finished[i]));
      this.emit('history', { type: 'history', sessions: rows.map((row) => this._sessionRecord(row)), tests });
    }

    // ---------------------------------------------------------------- commands

    _id(prefix) {
      return `dash-${prefix}-${Date.now().toString(36)}-${++this.rid}`;
    }

    async _http(method, path, body) {
      let res;
      try {
        res = await fetch(this.base + path, {
          method,
          headers: Object.assign(body ? { 'content-type': 'application/json' } : {},
            this.authorization ? { Authorization: this.authorization } : {},
            this.client ? { 'X-HAVen-Client': this.client } : {}),
          body: body ? JSON.stringify(body) : undefined,
        });
      } catch (e) {
        return { ok: false, error: 'Cannot reach the backend.' };
      }
      let data = null;
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      if (!res.ok) {
        const d = data && data.detail;
        const text = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `Backend error ${res.status}`;
        return { ok: false, status: res.status, error: text };
      }
      return { ok: true, data };
    }

    async send(name, args = {}) {
      const id = this._id('cmd');
      const done = (extra) => ({ type: 'response', command_id: id, command: name, status: 'accepted', ...extra });
      const fail = (error) => ({ type: 'response', command_id: id, command: name, status: 'error', error });
      if (!this.isOpen) return fail('Not connected to the backend.');

      if (name === 'start_session') {
        const r = await this._http('POST', '/api/sessions', { runtime: this.runtime, label: 'Dashboard recording', request_id: id });
        if (!r.ok) return fail(r.error);
        const row = r.data || {};
        this.scoreBase = row.score && row.score.at_start ? num(row.score.at_start.cumulative_au) : null;
        this.sessionId = row.session_id || null;
        this._refresh();
        return done({ session_id: row.session_id });
      }
      if (name === 'stop_session') {
        const sid = this.sessionId || (this.status && this.status.active_session_id);
        if (!sid) return fail('No recording is running.');
        const r = await this._http('POST', `/api/sessions/${encodeURIComponent(sid)}/stop`, { request_id: id });
        if (!r.ok) {
          // the backend may already have ended it (the source ended, the sleeve changed): that is not a failure
          const g = await this._http('GET', `/api/sessions/${encodeURIComponent(sid)}`);
          if (g.ok && g.data && g.data.state && g.data.state !== 'recording') {
            this.sessionId = null;
            this._sessionEnded(g.data);
            this._refresh();
            return done({ session_id: sid, already_ended: true });
          }
          return fail(r.error);
        }
        this.sessionId = null;
        this._sessionEnded(r.data);
        this._refresh();
        return done({ session_id: sid });
      }
      if (name === 'calibrate_relaxed' || name === 'calibrate_squeeze') {
        if (this.calibrating) return fail('A calibration step is already running.');
        this._calibrateStep(name === 'calibrate_relaxed' ? 'relaxed' : 'squeeze');
        return done();
      }
      if (name === 'calibrate_cancel') {
        this.calCancelled = true;
        const r = await this._http('POST', `/api/runtimes/${encodeURIComponent(this.runtime)}/calibration`,
          { action: 'cancel', request_id: this._id('cal') });
        this.emit('calibration', { type: 'calibration', device_id: this._dev(), phase: 'cancelled' });
        return r.ok ? done() : fail(r.error);
      }
      if (name === 'new_shift') {
        const r = await this._http('POST', `/api/runtimes/${encodeURIComponent(this.runtime)}/tool-use/new-shift`,
          { request_id: this._id('shift') });
        return r.ok ? done(r.data) : fail(r.error);
      }
      if (name === 'start_station_test') {
        const r = await this.station.start(args.mode);
        return r.error ? fail(r.error) : done({ test_id: r.test_id });
      }
      if (name === 'abort_station_test') {
        const r = await this.station.abort();
        return r.error ? fail(r.error) : done();
      }
      if (name === 'submit_response') {
        const r = await this.station.respond(args.trial_id, args.response);
        return r.error ? fail(r.error) : done();
      }
      return fail(`Unknown command: ${name}`);
    }

    /** One calibration step (the guide's phone flow): send the command, count down while the backend captures,
     *  then report the processor's state: relaxed_captured / calibrated, or failed with the reason in plain words. */
    async _calibrateStep(action) {
      this.calibrating = true;
      this.calCancelled = false;
      const phase = action === 'relaxed' ? 'relax' : 'squeeze';
      const want = action === 'relaxed' ? 'relaxed_captured' : 'calibrated';
      const fail = (reason) => this.emit('calibration', { type: 'calibration', device_id: this._dev(), phase: 'failed', reason });
      try {
        const r = await this._http('POST', `/api/runtimes/${encodeURIComponent(this.runtime)}/calibration`,
          { action, request_id: this._id('cal') });
        if (!r.ok) { fail(r.error); return; }
        const t0 = performance.now();
        for (;;) { // the countdown for the wearer; the backend does the real capture
          if (this.calCancelled) return;
          const left = Math.max(0, CAPTURE_MS - (performance.now() - t0));
          this.emit('calibration', { type: 'calibration', device_id: this._dev(), phase, phase_ms: CAPTURE_MS, remaining_ms: left });
          if (left <= 0) break;
          await sleep(100);
        }
        let state = null;
        for (let i = 0; i < 30; i++) { // wait for the processor to finish the capture and say how it went
          if (this.calCancelled) return;
          const s = await this._http('GET', `/api/runtimes/${encodeURIComponent(this.runtime)}`);
          if (s.ok && s.data) {
            this._applyStatus(s.data);
            state = s.data.calibration || {};
            if (!String(state.state).startsWith('capturing')) break;
          }
          await sleep(150);
        }
        if (!state || state.state !== want) { fail(reasonText(state && state.reason)); return; }
        if (action === 'relaxed') {
          this.emit('calibration', { type: 'calibration', device_id: this._dev(), phase: 'relaxed_captured' });
          return;
        }
        const c = await this._http('GET', `/api/calibrations?device_id=${encodeURIComponent(this._dev() || '')}`);
        const rec = c.ok && Array.isArray(c.data) && c.data.length ? c.data[0] : null;
        const ref = rec && rec.record && rec.record.reference;
        this.emit('calibration', {
          type: 'calibration', device_id: this._dev(), phase: 'done',
          result: {
            rest_env: ref ? first(ref.relaxed) : null,
            max_env: ref ? first(ref.squeeze) : null,
            calibrated_at: rec ? rec.created_at : new Date().toISOString(),
          },
        });
      } finally {
        this.calibrating = false;
        this._refresh();
      }
    }

    _dev() {
      return this.status ? this.status.device_id : null;
    }
  }

  VG.BackendAdapter = BackendAdapter;
})();
