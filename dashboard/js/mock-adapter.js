/*
 * MockAdapter: fake sleeve + station that speaks the contract in contract.js.
 * Interface (same as WebSocketAdapter):
 *   connect(), disconnect(), send(name, args) -> Promise<response>, on(channel, fn)
 * Mock-only extras for testing: setSqueeze(bool), setVibration(bool),
 *   simulateDrop(), injectError()
 *
 * The station logic here is a stand-in so the screens can be built. Fahad owns
 * the real test behaviour; the score formula is a placeholder.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const CAL_PHASE_MS = 3000;
  const RESPONSE_MS = 4000;

  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  const round = (x, dp) => Math.round(x * 10 ** dp) / 10 ** dp;
  const mean = (a) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : NaN);
  const rand = (a, b) => a + Math.random() * (b - a);
  const gauss = () => {
    let u = 0, v = 0;
    while (!u) u = Math.random();
    while (!v) v = Math.random();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };
  const shortId = (p) => `${p}-${Date.now().toString(36).slice(-5)}`;

  class MockAdapter extends VG.Emitter {
    constructor() {
      super();
      this.kind = 'mock';
      this.ids = { sleeve: 'sleeve-mock-01', station: 'station-mock-01' };
      this.rateHz = 20;
      this.seq = 0;
      this.states = { sleeve: 'disconnected', station: 'disconnected' };
      this.squeezing = false;
      this.vibrating = false;
      this.effort = 0.05;
      this.cal = null;
      this.calRun = null;
      this.session = null;
      this.test = null;
      this.timer = null;
      this.dropped = false;
      this.cmdSeq = 0;
      this.relaxedLevel = null;
      this.tool = { state: 'unknown', onFor: 0, offFor: 0, session: 0, bouts: 0, bout: 0, longest: 0, observed: 0, sinceEmit: 0 };
    }

    // ---- connection ----
    connect() {
      if (this.timer) return;
      clearTimeout(this._dropTimer);
      this.dropped = false;
      this.timer = setInterval(() => this._tick(), 1000 / this.rateHz);
      this.emit('connection', { type: 'connection', connected: true });
      this._setState('sleeve', 'ready');
      this._setState('station', 'ready');
    }

    disconnect(reason = 'Disconnected by user') {
      clearInterval(this.timer);
      clearTimeout(this._dropTimer);
      this.timer = null;
      this.dropped = false;
      this._endEverything(reason);
      this._setState('sleeve', 'disconnected', reason);
      this._setState('station', 'disconnected', reason);
      this.emit('connection', { type: 'connection', connected: false, reason });
    }

    // ---- mock-only controls ----
    setSqueeze(on) { this.squeezing = !!on; }
    setVibration(on) { this.vibrating = !!on; }

    // Data stops silently (like a pulled cable); the disconnect is only noticed 3 s later.
    simulateDrop() {
      if (!this.timer) return;
      clearInterval(this.timer);
      this.timer = null;
      this.dropped = true;
      this._dropTimer = setTimeout(() => {
        if (this.dropped) this.disconnect('Lost connection to the sleeve');
      }, 3000);
    }

    injectError() {
      if (this.states.sleeve === 'disconnected') return;
      this._endEverything('Sleeve error');
      this._setState('sleeve', 'error', 'EMG lead appears to be off. Check the electrodes, then disconnect and reconnect.');
    }

    // ---- commands ----
    send(name, args = {}) {
      const cmd = { type: 'command', id: `cmd-${++this.cmdSeq}`, name, args };
      return new Promise((resolve) => setTimeout(() => resolve(this._handle(cmd)), 60));
    }

    _reply(cmd, status, extra = {}) {
      const r = { type: 'response', command_id: cmd.id, command: cmd.name, status, ...extra };
      this.emit('response', r);
      return r;
    }

    _handle(cmd) {
      const s = this.states.sleeve;
      const st = this.states.station;
      switch (cmd.name) {
        case 'calibrate_relaxed':
          if (s !== 'ready') return this._reply(cmd, 'error', { error: `The sleeve is ${s}. It must be ready to calibrate.` });
          this.relaxedLevel = null;
          this._startCal('relax');
          return this._reply(cmd, 'accepted');
        case 'calibrate_squeeze':
          if (this.relaxedLevel == null) return this._reply(cmd, 'error', { error: 'Capture the relaxed level first.' });
          this._startCal('squeeze');
          return this._reply(cmd, 'accepted');
        case 'calibrate_cancel':
          this.calRun = null;
          this.relaxedLevel = null;
          this._setState('sleeve', 'ready');
          this.emit('calibration', { type: 'calibration', device_id: this.ids.sleeve, phase: 'cancelled' });
          return this._reply(cmd, 'completed');
        case 'new_shift': {
          const before = { session_tool_s: Math.round(this.tool.session * 100) / 100, bouts: this.tool.bouts };
          Object.assign(this.tool, { session: 0, bouts: 0, longest: 0, bout: 0 });
          this.emit('event', { event: 'tool_session_reset', stream: 'imu', detail: before, at: new Date().toISOString() });
          return this._reply(cmd, 'completed', { previous: before });
        }
        case 'start_session':
          if (s !== 'ready') return this._reply(cmd, 'error', { error: `The sleeve is ${s}. It must be ready to record.` });
          this.session = {
            session_id: shortId('ses'),
            started_at: new Date().toISOString(),
            t0: performance.now(),
            total: 0,
            peakVib: 0,
            actSum: 0,
            actN: 0,
            calibrated: !!this.cal,
          };
          this._setState('sleeve', 'recording');
          return this._reply(cmd, 'accepted', { session_id: this.session.session_id });
        case 'stop_session': {
          if (!this.session) return this._reply(cmd, 'error', { error: 'No session is recording.' });
          const rec = this._saveSession('stopped');
          this._setState('sleeve', 'ready');
          return this._reply(cmd, 'completed', { session_id: rec.session_id });
        }
        case 'start_station_test':
          if (st !== 'ready') return this._reply(cmd, 'error', { error: `The station is ${st}. It must be ready to start a test.` });
          this._startTest(cmd.args.mode);
          return this._reply(cmd, 'accepted', { test_id: this.test.test_id });
        case 'abort_station_test':
          if (!this.test) return this._reply(cmd, 'error', { error: 'No test is running.' });
          this._abortTest('Aborted by operator', true);
          return this._reply(cmd, 'completed');
        case 'submit_response':
          if (!this.test || !this.test.current || this.test.current.trial_id !== cmd.args.trial_id) {
            return this._reply(cmd, 'error', { error: 'That trial is no longer waiting for a response.' });
          }
          this._resolveTrial(cmd.args.response === 'felt' ? 'felt' : 'not_felt');
          return this._reply(cmd, 'completed');
        default:
          return this._reply(cmd, 'error', { error: `Unknown command: ${cmd.name}` });
      }
    }

    // ---- telemetry ----
    _tick() {
      const dt = 1 / this.rateHz;
      const autoSqueeze = this.calRun && this.calRun.phase === 'squeeze';
      const target = this.squeezing || autoSqueeze ? 0.9 : 0.06;
      this.effort += (target - this.effort) * 0.25;
      const effort = Math.max(0, this.effort + gauss() * 0.02);

      let env = 35 + effort * 620 + Math.abs(gauss()) * 8;
      const artifact = this.vibrating;
      if (artifact) env += 25 + Math.abs(gauss()) * 45; // tool vibration leaking into EMG
      const vib = this.vibrating ? Math.max(0, 4.1 + gauss() * 0.5) : Math.max(0, 0.06 + gauss() * 0.02);

      let act = null;
      if (this.cal) act = Math.max(0, ((env - this.cal.rest) / (this.cal.max - this.cal.rest)) * 100);   // may exceed 100
      // PLACEHOLDER experimental formula: vibration weighted up by grip. Not validated.
      const scoreRate = act == null ? null : vib * (1 + act / 100);

      if (this.session) {
        if (scoreRate != null) this.session.total += (scoreRate * dt) / 60;
        this.session.peakVib = Math.max(this.session.peakVib, vib);
        if (act != null) { this.session.actSum += act; this.session.actN++; }
      }
      if (this.calRun) this._calTick(env);

      this.seq++;
      this.emit('telemetry', {
        type: 'telemetry',
        device_id: this.ids.sleeve,
        device_role: 'sleeve',
        seq: this.seq,
        t_ms: Math.round(performance.now()),
        sample_rate_hz: this.rateHz,
        provenance: 'simulated',
        recording_id: null,
        values: {
          emg_env: round(env, 1),
          activation_pct: act == null ? null : round(act, 1),
          activation_bar_pct: act == null ? null : round(clamp(act, 0, 100), 1),
          vib_ahv_est: round(vib, 2),
          score_rate: scoreRate == null ? null : round(scoreRate, 2),
          score_total: this.session && this.session.calibrated ? round(this.session.total, 2) : null,
        },
        units: { emg_env: 'a.u.', activation_pct: '%MVC', vib_ahv_est: 'm/s²', score_rate: 'pts/min', score_total: 'pts' },
        quality: { emg_vibration_artifact: artifact, imu_saturated: false, calibrated: !!this.cal,
                   above_max: act != null && act > 100, below_rest: false, degraded: false },
      });
      this._toolTick();
    }

    // ---- tool use (mock: "Tool running" means a tool is vibrating) ----
    _toolTick() {
      const t = this.tool;
      const nowMs = performance.now();
      const dt = t.lastAt ? Math.min(1, (nowMs - t.lastAt) / 1000) : 0;   // real elapsed time: timers get throttled
      t.lastAt = nowMs;
      t.observed += dt;
      if (this.vibrating) { t.onFor += dt; t.offFor = 0; } else { t.offFor += dt; t.onFor = 0; }
      if (t.state !== 'tool' && t.onFor >= 2) { t.state = 'tool'; t.bouts += 1; t.bout = 0; }
      else if (t.state === 'tool' && t.offFor >= 1.5) { t.state = 'no_tool'; }
      else if (t.state === 'unknown' && t.observed > 1) t.state = 'no_tool';
      if (t.state === 'tool') { t.session += dt; t.bout += dt; t.longest = Math.max(t.longest, t.bout); }
      if ((t.sinceEmit += dt) >= 0.5) {
        t.sinceEmit = 0;
        const warnings = [];
        if (t.state === 'tool' && t.bout >= 60) warnings.push('continuous_tool_use_long');
        this.emit('tool', {
          type: 'tool_use', state: t.state, status: 'ok', saved: false,
          tool_band_rms_ms2: this.vibrating ? 4.1 : 0.05, dominant_hz: this.vibrating ? 120 : null,
          bout_s: t.state === 'tool' ? t.bout : 0, bouts: t.bouts, longest_bout_s: t.longest,
          session_tool_s: t.session, session_observed_s: t.observed, warnings,
        });
      }
    }

    // ---- calibration (two steps, like the real processor) ----
    _startCal(phase) {
      this._setState('sleeve', 'calibrating');
      this.calRun = { phase, phaseStart: performance.now(), relax: [], squeeze: [] };
      this._emitCal();
    }

    _calTick(env) {
      const r = this.calRun;
      const now = performance.now();
      r[r.phase].push(env);
      if (now - r.phaseStart >= CAL_PHASE_MS) {
        if (r.phase === 'relax') {
          this.relaxedLevel = mean(r.relax.slice(Math.floor(r.relax.length / 3)));
          this.calRun = null;
          this._setState('sleeve', 'ready');
          this.emit('calibration', { type: 'calibration', device_id: this.ids.sleeve, phase: 'relaxed_captured' });
        } else {
          this._finishCal();
        }
        return;
      }
      this._emitCal();
    }

    _emitCal() {
      const r = this.calRun;
      this.emit('calibration', {
        type: 'calibration',
        device_id: this.ids.sleeve,
        phase: r.phase,
        phase_ms: CAL_PHASE_MS,
        remaining_ms: Math.max(0, Math.round(CAL_PHASE_MS - (performance.now() - r.phaseStart))),
      });
    }

    _finishCal() {
      const r = this.calRun;
      this.calRun = null;
      const rest = this.relaxedLevel;
      this.relaxedLevel = null;
      const sorted = r.squeeze.slice().sort((a, b) => a - b);
      const max = sorted[Math.floor(sorted.length * 0.9)];
      if (!(max - rest > 100)) {
        this.emit('calibration', {
          type: 'calibration', device_id: this.ids.sleeve, phase: 'failed',
          reason: 'The squeeze wasn’t clearly stronger than rest. Squeeze harder, or check the pads, and try again.',
        });
      } else {
        this.cal = { rest, max, at: new Date().toISOString() };
        this.emit('calibration', {
          type: 'calibration', device_id: this.ids.sleeve, phase: 'done',
          result: { rest_env: round(rest, 1), max_env: round(max, 1), calibrated_at: this.cal.at },
        });
      }
      this._setState('sleeve', 'ready');
    }

    // ---- sessions ----
    _saveSession(endedReason) {
      const s = this.session;
      this.session = null;
      const rec = {
        type: 'session_saved',
        session_id: s.session_id,
        device_id: this.ids.sleeve,
        provenance: 'simulated',
        started_at: s.started_at,
        ended_at: new Date().toISOString(),
        duration_s: Math.round((performance.now() - s.t0) / 1000),
        ended_reason: endedReason,
        calibrated: s.calibrated,
        score_total: s.calibrated ? round(s.total, 2) : null,
        peak_vib_ahv_est: round(s.peakVib, 2),
        mean_activation_pct: s.actN ? round(s.actSum / s.actN, 1) : null,
      };
      this.emit('session', rec);
      return rec;
    }

    // ---- station (stand-in for Fahad's engine) ----
    _startTest(mode) {
      const preview = mode !== 'full';
      this.test = {
        test_id: shortId('st'),
        mode: preview ? 'preview' : 'full',
        setting: preview ? 150 : 180,
        step: preview ? 40 : 20,
        trials: [],
        reversals: [],
        lastDir: null,
        current: null,
        timer: null,
        maxTrials: preview ? 6 : 40,
        catchAt: new Set(preview ? [4] : [4, 11, 19, 27]),
        started_at: new Date().toISOString(),
      };
      this._setState('station', 'testing');
      this.emit('station', {
        type: 'station_started',
        test_id: this.test.test_id,
        mode: this.test.mode,
        planned_trials: preview ? this.test.maxTrials : null,
        response_window_ms: RESPONSE_MS,
      });
      this._scheduleTrial();
    }

    _scheduleTrial() {
      this.test.timer = setTimeout(() => this._runTrial(), rand(900, 2200)); // random gap
    }

    _runTrial() {
      const t = this.test;
      if (!t) return;
      const index = t.trials.length + 1;
      const isCatch = t.catchAt.has(index);
      const trial = {
        type: 'station_trial',
        test_id: t.test_id,
        trial_id: `${t.test_id}-t${index}`,
        index,
        stimulus_setting: isCatch ? 0 : t.setting,
        measured_amplitude: null,
        is_catch: isCatch,
        response_window_ms: RESPONSE_MS,
      };
      t.current = { ...trial, deliveredAt: performance.now() };
      this.emit('station', trial);
      t.timer = setTimeout(() => this._resolveTrial('missing'), RESPONSE_MS);
    }

    _resolveTrial(response) {
      const t = this.test;
      const tr = t && t.current;
      if (!tr) return;
      clearTimeout(t.timer);
      t.current = null;
      const rec = {
        type: 'station_trial_result',
        test_id: t.test_id,
        trial_id: tr.trial_id,
        index: tr.index,
        stimulus_setting: tr.stimulus_setting,
        measured_amplitude: null,
        is_catch: tr.is_catch,
        response,
        rt_ms: response === 'missing' ? null : Math.round(performance.now() - tr.deliveredAt),
      };
      t.trials.push(rec);
      // 1-up/1-down staircase; catch trials and missing responses don't move it.
      if (!tr.is_catch && response !== 'missing') {
        const dir = response === 'felt' ? -1 : 1;
        if (t.lastDir !== null && dir !== t.lastDir) t.reversals.push(tr.stimulus_setting);
        t.lastDir = dir;
        t.setting = clamp(t.setting + dir * t.step, 10, 255);
      }
      this.emit('station', rec);
      const done = t.mode === 'full' ? t.reversals.length >= 8 || t.trials.length >= t.maxTrials : t.trials.length >= t.maxTrials;
      if (done) this._completeTest();
      else this._scheduleTrial();
    }

    _completeTest() {
      const t = this.test;
      this.test = null;
      const used = t.mode === 'full' ? t.reversals.slice(-6) : t.reversals;
      const catches = t.trials.filter((x) => x.is_catch);
      this._setState('station', 'completed');
      this.emit('station', {
        type: 'station_complete',
        test_id: t.test_id,
        device_id: this.ids.station,
        provenance: 'simulated',
        mode: t.mode,
        started_at: t.started_at,
        completed_at: new Date().toISOString(),
        threshold_setting: used.length ? round(mean(used), 1) : null,
        reversals: used,
        n_reversals: t.reversals.length,
        catch_trials: catches.length,
        catch_trials_felt: catches.filter((x) => x.response === 'felt').length,
        missing_responses: t.trials.filter((x) => x.response === 'missing').length,
        measured_amplitude_available: false,
        trials: t.trials,
      });
      setTimeout(() => {
        if (this.states.station === 'completed') this._setState('station', 'ready');
      }, 300);
    }

    _abortTest(reason, backToReady) {
      const t = this.test;
      if (!t) return;
      clearTimeout(t.timer);
      this.test = null;
      this.emit('station', { type: 'station_aborted', test_id: t.test_id, reason });
      if (backToReady) this._setState('station', 'ready');
    }

    // ---- helpers ----
    _endEverything(reason) {
      if (this.calRun) {
        this.calRun = null;
        this.emit('calibration', { type: 'calibration', device_id: this.ids.sleeve, phase: 'failed', reason: `${reason}. Calibration was not saved.` });
      }
      if (this.session) this._saveSession(reason);
      if (this.test) this._abortTest(reason, false);
    }

    _setState(role, state, detail = '') {
      this.states[role] = state;
      this.emit('state', { type: 'state', device_id: this.ids[role], device_role: role, state, detail });
    }
  }

  VG.MockAdapter = MockAdapter;
})();
