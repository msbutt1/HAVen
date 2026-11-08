/*
 * StationRunner: the perception-test screens on top of the backend's station API.
 *
 * The backend handles ONE trial at a time (deliver a stimulus, wait for the answer, time it, store it) and
 * drives a SIMULATED controller: no motor is moved. The test sequence (a 1-up/1-down staircase with catch
 * trials) is a stand-in that lives here until Fahad's protocol engine is wired to the backend. Every session
 * is stored with source "simulated", and the screens say so. Motor settings are settings, never measured
 * amplitudes, and the result is a prototype, not a diagnostic.
 *
 * Contract with the screens (see contract.js): emits station_started / station_trial /
 * station_trial_result / station_complete / station_aborted on the adapter's 'station' channel.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const RESPONSE_MS = 4000;
  const UNITS = 'pwm_duty_0_255';
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const rand = (a, b) => a + Math.random() * (b - a);
  const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;

  // The staircase rule, shared by a live test and by sessions loaded from the backend's history:
  // a reversal is recorded when the direction of the answers changes; catch trials and missing answers do not move it.
  function summarize(trials, mode) {
    let lastDir = null;
    const reversals = [];
    for (const t of trials) {
      if (t.is_catch || t.response === 'missing') continue;
      const dir = t.response === 'felt' ? -1 : 1;
      if (lastDir !== null && dir !== lastDir) reversals.push(t.stimulus_setting);
      lastDir = dir;
    }
    const used = mode === 'full' ? reversals.slice(-6) : reversals;
    const catches = trials.filter((x) => x.is_catch);
    return {
      reversals: used,
      n_reversals: reversals.length,
      threshold_setting: used.length ? Math.round(mean(used) * 10) / 10 : null,
      catch_trials: catches.length,
      catch_trials_felt: catches.filter((x) => x.response === 'felt').length,
      missing_responses: trials.filter((x) => x.response === 'missing').length,
    };
  }

  class StationRunner {
    constructor(adapter) {
      this.a = adapter;
      this.t = null; // the running test
      this.gapMs = adapter.stationGapMs || [900, 2200]; // random gap between trials
    }

    get running() {
      return !!this.t;
    }

    async start(mode) {
      if (this.t) return { error: 'A test is already running.' };
      const preview = mode !== 'full';
      const t = {
        mode: preview ? 'preview' : 'full',
        setting: preview ? 150 : 180,
        step: preview ? 40 : 20,
        maxTrials: preview ? 6 : 40,
        catchAt: new Set(preview ? [4] : [4, 11, 19, 27]),
        trials: [], lastDir: null, reversals: [], open: null, cancelled: false,
        started_at: new Date().toISOString(), sid: null,
      };
      const r = await this.a._http('POST', '/api/station/sessions', {
        source: 'simulated',
        label: `Dashboard ${t.mode} test`,
        protocol: { kind: 'staircase_1up_1down', mode: t.mode, start_setting: t.setting, step: t.step, units: UNITS,
                    note: 'dashboard stand-in sequence; not a validated procedure' },
        request_id: this.a._id('st'),
      });
      if (!r.ok) return { error: r.error };
      t.sid = r.data.station_session_id;
      this.t = t;
      this.a._setStation('testing');
      this.a.emit('station', { type: 'station_started', test_id: t.sid, mode: t.mode,
                               planned_trials: preview ? t.maxTrials : null, response_window_ms: RESPONSE_MS });
      this._loop(t);
      return { test_id: t.sid };
    }

    async _pause(t, ms) { // cancellable wait
      const end = Date.now() + ms;
      while (!t.cancelled && Date.now() < end) await new Promise((r) => setTimeout(r, Math.min(100, end - Date.now())));
    }

    async _loop(t) {
      try {
        while (!t.cancelled) {
          await this._pause(t, rand(this.gapMs[0], this.gapMs[1])); // random gap, so the wearer cannot anticipate the next trial
          if (t.cancelled) return;
          const index = t.trials.length + 1;
          const isCatch = t.catchAt.has(index);
          const setting = isCatch ? 0 : t.setting;
          const p = await this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/stimulus`, {
            stimulus: { motor_setting: setting, units: UNITS }, is_catch: isCatch,
            response_timeout_s: RESPONSE_MS / 1000, request_id: this.a._id('stim'),
          });
          if (t.cancelled) return;
          if (!p.ok) { await this._fail(t, p.error); return; }
          const trialId = p.data.trial_id;
          t.open = { trial_id: trialId, index, setting, is_catch: isCatch, resolve: null, timer: null };
          this.a.emit('station', { type: 'station_trial', test_id: t.sid, trial_id: trialId, index, stimulus_setting: setting,
                                   measured_amplitude: null, is_catch: isCatch, response_window_ms: RESPONSE_MS });
          const answer = await new Promise((resolve) => {
            t.open.resolve = resolve;
            t.open.timer = setTimeout(() => resolve({ timeout: true }), RESPONSE_MS + 400);
          });
          clearTimeout(t.open.timer);
          t.open = null;
          if (t.cancelled) return;
          let response = answer.response;
          let rt = answer.rt;
          if (answer.timeout) { // the backend timed it out and stored it: read what it stored (never assume "not felt")
            const s = await this.a._http('GET', `/api/station/sessions/${encodeURIComponent(t.sid)}`);
            const rec = s.ok && s.data && Array.isArray(s.data.trials) ? s.data.trials[index - 1] : null;
            response = rec ? rec.response : 'missing';
            rt = rec && rec.response_latency_ms != null ? Math.round(rec.response_latency_ms) : null;
          }
          const res = { type: 'station_trial_result', test_id: t.sid, trial_id: trialId, index, stimulus_setting: setting,
                        measured_amplitude: null, is_catch: isCatch, response, rt_ms: response === 'missing' ? null : rt };
          t.trials.push(res);
          if (!isCatch && response !== 'missing') {
            const dir = response === 'felt' ? -1 : 1;
            if (t.lastDir !== null && dir !== t.lastDir) t.reversals.push(setting);
            t.lastDir = dir;
            t.setting = clamp(t.setting + dir * t.step, 10, 255);
          }
          this.a.emit('station', res);
          const done = t.mode === 'full' ? t.reversals.length >= 8 || t.trials.length >= t.maxTrials : t.trials.length >= t.maxTrials;
          if (done) { await this._complete(t); return; }
        }
      } catch (e) {
        if (!t.cancelled) await this._fail(t, 'The test stopped unexpectedly.');
      }
    }

    async _complete(t) {
      const r = await this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/complete`, { request_id: this.a._id('stc') });
      if (!r.ok) { await this._fail(t, r.error); return; }
      this.t = null;
      const sum = summarize(t.trials, t.mode);
      this.a._setStation('completed');
      this.a.emit('station', {
        type: 'station_complete', test_id: t.sid, device_id: 'station-simulated', provenance: 'simulated', mode: t.mode,
        started_at: t.started_at, completed_at: new Date().toISOString(), ...sum,
        measured_amplitude_available: false, trials: t.trials,
      });
      setTimeout(() => { this.a._setStation('ready'); }, 300);
    }

    async _fail(t, error) {
      if (this.t !== t) return;
      t.cancelled = true;
      this.t = null;
      await this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/abort`, { reason: error, request_id: this.a._id('sta') });
      this.a.emit('station', { type: 'station_aborted', test_id: t.sid, reason: error });
      this.a._setStation('ready');
    }

    async abort(reason = 'Aborted by operator') {
      const t = this.t;
      if (!t) return { error: 'No test is running.' };
      t.cancelled = true;
      this.t = null;
      if (t.open) { clearTimeout(t.open.timer); t.open.resolve({ timeout: true }); }
      const r = await this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/abort`, { reason, request_id: this.a._id('sta') });
      this.a.emit('station', { type: 'station_aborted', test_id: t.sid, reason: r.ok ? reason : `${reason} (the backend could not confirm the stop)` });
      this.a._setStation('ready');
      return { ok: true };
    }

    /** The connection to the backend dropped: stop the sequence; the screens already say so. */
    connectionLost() {
      const t = this.t;
      if (!t) return;
      t.cancelled = true;
      this.t = null;
      if (t.open) { clearTimeout(t.open.timer); t.open.resolve({ timeout: true }); }
      this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/abort`, { reason: 'connection lost' }); // best effort
    }

    async respond(trialId, response) {
      const t = this.t;
      if (!t || !t.open || t.open.trial_id !== trialId) return { error: 'No trial is waiting for an answer.' };
      if (response !== 'felt' && response !== 'not_felt') return { error: 'The answer must be felt or not_felt.' };
      const r = await this.a._http('POST', `/api/station/sessions/${encodeURIComponent(t.sid)}/respond`, { response, request_id: this.a._id('sr') });
      if (!r.ok) return { error: r.error };
      if (t.open && t.open.trial_id === trialId) {
        t.open.resolve({ response: r.data.response, rt: Math.round(r.data.response_latency_ms) });
      }
      return { ok: true };
    }
  }

  VG.StationRunner = StationRunner;
  VG.stationSummarize = summarize;
})();
