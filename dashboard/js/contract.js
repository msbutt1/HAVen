/*
 * HAVen dashboard: data contract (PLACEHOLDER)
 *
 * This is the dashboard's working guess at the message format, built from
 * Ali_04_interfaces_and_parallelism.md. Replace it with the team's agreed
 * "device telemetry contract v1" as soon as that is final. Both adapters
 * (mock-adapter.js and ws-adapter.js) emit exactly these shapes, so only
 * this file and the adapters should need to change.
 *
 * Every message is JSON with a "type" field. The adapters emit them on
 * these channels: telemetry, state, connection, calibration, session,
 * station, response.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const PROVENANCE = ['live', 'replay', 'simulated'];
  const DEVICE_STATES = ['disconnected', 'ready', 'calibrating', 'recording', 'testing', 'completed', 'error'];
  const COMMANDS = [
    'calibrate',
    'start_session',
    'stop_session',
    'start_station_test',
    'abort_station_test',
    'submit_response',
  ];

  // Example payloads. Share these with Abduljabbar (backend) and Fahad (station).
  const EXAMPLES = {
    telemetry: {
      type: 'telemetry',
      device_id: 'sleeve-01',
      device_role: 'sleeve',
      seq: 1042,
      t_ms: 523310, // device or backend timebase, milliseconds
      sample_rate_hz: 20, // rate of these dashboard messages, not the raw ADC rate
      provenance: 'live', // live | replay | simulated
      recording_id: null, // set when provenance is "replay"
      values: {
        emg_env: 412.5, // EMG envelope, arbitrary units
        activation_pct: 61.2, // % of calibrated max squeeze, null if not calibrated
        vib_ahv_est: 3.84, // estimated wrist vibration, m/s²
        score_rate: 6.19, // EXPERIMENTAL score rate, null if not calibrated
        score_total: 12.4, // EXPERIMENTAL session total, null if not recording
      },
      units: { emg_env: 'a.u.', activation_pct: '%MVC', vib_ahv_est: 'm/s²', score_rate: 'pts/min', score_total: 'pts' },
      quality: { emg_vibration_artifact: false, imu_saturated: false, calibrated: true },
    },
    state: {
      type: 'state',
      device_id: 'sleeve-01',
      device_role: 'sleeve', // sleeve | station
      state: 'ready', // one of DEVICE_STATES
      detail: '',
    },
    command: { type: 'command', id: 'cmd-17', name: 'start_station_test', args: { mode: 'preview' } },
    response: { type: 'response', command_id: 'cmd-17', command: 'start_station_test', status: 'accepted', test_id: 'st-8k2' },
    calibration: { type: 'calibration', device_id: 'sleeve-01', phase: 'squeeze', phase_ms: 3000, remaining_ms: 1200 },
    calibration_done: {
      type: 'calibration',
      device_id: 'sleeve-01',
      phase: 'done', // relax | squeeze | done | failed
      result: { rest_env: 48.2, max_env: 590.1, calibrated_at: '2026-11-07T18:52:10Z' },
    },
    session_saved: {
      type: 'session_saved',
      session_id: 'ses-01',
      device_id: 'sleeve-01',
      provenance: 'live',
      started_at: '2026-11-07T18:53:00Z',
      ended_at: '2026-11-07T18:55:30Z',
      duration_s: 150,
      ended_reason: 'stopped', // stopped | or a reason such as a lost connection
      calibrated: true,
      score_total: 14.2,
      peak_vib_ahv_est: 4.9,
      mean_activation_pct: 38.5,
    },
    station_started: { type: 'station_started', test_id: 'st-8k2', mode: 'preview', planned_trials: 6, response_window_ms: 4000 },
    station_trial: {
      type: 'station_trial',
      test_id: 'st-8k2',
      trial_id: 'st-8k2-t3',
      index: 3,
      stimulus_setting: 110, // motor drive setting 0..255, NOT a measured amplitude
      measured_amplitude: null, // only when an IMU on the station measures it
      is_catch: false, // the dashboard hides this until the test is complete
      response_window_ms: 4000,
    },
    station_trial_result: {
      type: 'station_trial_result',
      test_id: 'st-8k2',
      trial_id: 'st-8k2-t3',
      index: 3,
      stimulus_setting: 110,
      measured_amplitude: null,
      is_catch: false,
      response: 'felt', // felt | not_felt | missing
      rt_ms: 812, // null when missing
    },
    station_complete: {
      type: 'station_complete',
      test_id: 'st-8k2',
      device_id: 'station-01',
      provenance: 'live',
      mode: 'preview',
      started_at: '2026-11-07T19:01:00Z',
      completed_at: '2026-11-07T19:01:40Z',
      threshold_setting: 95.0, // null if there were not enough reversals
      reversals: [110, 70, 110],
      n_reversals: 3,
      catch_trials: 1,
      catch_trials_felt: 0,
      missing_responses: 0,
      measured_amplitude_available: false,
      trials: [],
    },
    station_aborted: { type: 'station_aborted', test_id: 'st-8k2', reason: 'Aborted by operator' },
  };

  const CHANNEL_FOR_TYPE = {
    telemetry: 'telemetry',
    state: 'state',
    calibration: 'calibration',
    session_saved: 'session',
    station_started: 'station',
    station_trial: 'station',
    station_trial_result: 'station',
    station_complete: 'station',
    station_aborted: 'station',
    response: 'response',
  };

  VG.contract = {
    VERSION: 'dashboard-placeholder-0',
    DEFAULT_WS_URL: 'ws://localhost:8000/ws',
    PROVENANCE,
    DEVICE_STATES,
    COMMANDS,
    EXAMPLES,
    channelFor: (type) => CHANNEL_FOR_TYPE[type] || null,
  };

  // Tiny event emitter shared by both adapters.
  VG.Emitter = class Emitter {
    constructor() {
      this._h = {};
    }
    on(channel, fn) {
      (this._h[channel] = this._h[channel] || []).push(fn);
      return () => {
        this._h[channel] = this._h[channel].filter((f) => f !== fn);
      };
    }
    emit(channel, msg) {
      (this._h[channel] || []).forEach((f) => f(msg));
    }
  };
})();
