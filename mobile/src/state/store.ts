/**
 * The app's state, fed by the web dashboard's adapter events with the same rules as dashboard/js/app.js. A module
 * singleton: screens read `store.s` and re-render through `useStore()` when it changes (at most ~10 times a second,
 * so 20 Hz telemetry does not redraw every screen 20 times a second).
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import { useSyncExternalStore } from 'react';

import { label } from '../vg/address.ts';
import type { Saved, SessionRec, TestRec } from '../vg/summary.ts';
import { type Adapter, VG } from './shared';

export const STALE_MS = 1000;

export interface Dev { state: string; detail: string }
export interface Pt { t: number; v: number | null }
export interface Telemetry {
  device_id?: string;
  provenance: 'live' | 'replay' | 'simulated';
  recording_id?: string | null;
  sample_rate_hz?: number;
  values: { emg_env?: number | null; activation_pct?: number | null; activation_bar_pct?: number | null;
    vib_ahv_est?: number | null; score_rate?: number | null; score_total?: number | null };
  quality?: { calibrated?: boolean; emg_vibration_artifact?: boolean; above_max?: boolean; below_rest?: boolean; degraded?: boolean };
  units?: Record<string, string>;
}
export interface DeviceEvent { event: string; text: string; at: string; detail?: any; warn: boolean }
export interface Trial { trial_id: string; index: number; stimulus_setting: number; is_catch: boolean; response: string;
  rt_ms: number | null }
export interface StationState {
  view: 'idle' | 'running' | 'complete';
  mode: 'preview' | 'full';
  planned: number | null;
  trials: Trial[];
  current: (Record<string, any> & { trial_id: string; index: number; receivedAt: number; answered: boolean;
    response_window_ms: number }) | null;
  result: any;
  msg: string;
}

export interface State {
  mode: 'none' | 'backend' | 'demo';
  base: string | null;
  connected: boolean;
  everConnected: boolean;
  problem: string | null;      // why the laptop does not answer, in plain words
  needsPassword: boolean;
  devices: { sleeve: Dev; station: Dev };
  latest: Telemetry | null;
  lastTelemetryAt: number;
  series: { act: Pt[]; vib: Pt[]; score: Pt[]; raw: Pt[] };
  calibration: { rest: number; max: number; at: string } | null;
  cal: { phase: string | null; remaining: number; phaseMs: number; msg: string };
  session: { id: string; startedAt: number } | null;
  busy: boolean;
  saved: Saved[];
  tool: any;
  events: DeviceEvent[];
  recalibrate: boolean;
  st: StationState;
  toast: string | null;
}

const disconnected = (detail = ''): Dev => ({ state: 'disconnected', detail });
const blankStation = (): StationState =>
  ({ view: 'idle', mode: 'preview', planned: null, trials: [], current: null, result: null, msg: '' });

const EVENT_TEXT: Record<string, string> = {
  boot_change: 'The wearable restarted. Calibrate again.',
  seq_gap: 'Some records were lost on the way.',
  sample_gap: 'A gap in the samples.',
  stream_stale: 'No data from the wearable: values are hidden.',
  stream_recovered: 'Data resumed.',
  invalid_record: 'A record failed the checks and was rejected.',
  tool_session_reset: 'New shift: tool time reset.',
  rate_mismatch: 'The sample rate differs from what the wearable announced.',
};
const WARN_EVENTS = new Set(['boot_change', 'stream_stale', 'seq_gap', 'invalid_record']);

function eventText(e: any): string {
  if (e.event === 'calibration_state') {
    const st = (e.detail && e.detail.state) || '';
    return st === 'failed' ? `Calibration failed: ${String(e.detail.reason || '').replace(/_/g, ' ')}.`
      : `Calibration: ${String(st).replace(/_/g, ' ')}.`;
  }
  return EVENT_TEXT[e.event] || String(e.event).replace(/_/g, ' ');
}

/** Sent as X-VibeGuard-Client: the laptop then answers a missing password with a plain 401 (no iOS prompt). */
const CLIENT = 'vibeguard-mobile';

const KEY_LAST = 'vg.lastBase';
const KEY_USER = 'vg.user';
const KEY_PASS = 'vg.password';

class Store {
  s: State = this.fresh();
  private adapter: Adapter | null = null;
  private listeners = new Set<() => void>();
  private version = 0;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private probeTimer: ReturnType<typeof setTimeout> | null = null;
  private toastTimer: ReturnType<typeof setTimeout> | null = null;

  private fresh(): State {
    return {
      mode: 'none', base: null, connected: false, everConnected: false, problem: null, needsPassword: false,
      devices: { sleeve: disconnected(), station: disconnected() },
      latest: null, lastTelemetryAt: 0,
      series: { act: [], vib: [], score: [], raw: [] },
      calibration: null, cal: { phase: null, remaining: 0, phaseMs: 3500, msg: '' },
      session: null, busy: false, saved: [], tool: null, events: [], recalibrate: false,
      st: blankStation(), toast: null,
    };
  }

  // ---- subscription ----
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getVersion = () => this.version;

  /** Coalesce changes: telemetry arrives 20 times a second; screens redraw at most ~10 times a second. */
  private changed(now = false) {
    if (now) {
      if (this.flushTimer) clearTimeout(this.flushTimer);
      this.flushTimer = null;
      this.version++;
      this.listeners.forEach((l) => l());
      return;
    }
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => this.changed(true), 100);
  }

  toast(text: string) {
    this.s.toast = text;
    if (this.toastTimer) clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => { this.s.toast = null; this.changed(true); }, 2600);
    this.changed(true);
  }

  // ---- saved laptop and password ----
  async lastLaptop(): Promise<{ base: string; user: string; password: string } | null> {
    const base = await AsyncStorage.getItem(KEY_LAST);
    if (!base) return null;
    return { base, user: (await SecureStore.getItemAsync(KEY_USER)) ?? '', password: (await SecureStore.getItemAsync(KEY_PASS)) ?? '' };
  }

  private async remember(base: string, user: string, password: string) {
    await AsyncStorage.setItem(KEY_LAST, base);
    if (user || password) {
      await SecureStore.setItemAsync(KEY_USER, user);
      await SecureStore.setItemAsync(KEY_PASS, password);
    } else {
      await SecureStore.deleteItemAsync(KEY_USER);
      await SecureStore.deleteItemAsync(KEY_PASS);
    }
  }

  // ---- connecting ----
  /** Open the laptop at `base` (http://host:port). `authorization` is "Basic …" or null. */
  connectLaptop(base: string, authorization: string | null, creds: { user: string; password: string }) {
    this.stop();
    this.s = { ...this.fresh(), mode: 'backend', base };
    this.attach(new VG.BackendAdapter({ base, authorization, client: CLIENT }));
    // If the live connection has not opened after a few seconds, find out why with a plain request.
    this.probeTimer = setTimeout(() => this.diagnose(base, authorization), 3500);
    this.adapterOnce('connection', (m) => {
      if (m.connected) this.remember(base, creds.user, creds.password).catch(() => {});
    });
    this.changed(true);
  }

  /** The practice feed: the web dashboard's simulated sleeve and station, with a labelled sample history. */
  connectDemo() {
    this.stop();
    this.s = { ...this.fresh(), mode: 'demo', base: null };
    this.attach(new VG.MockAdapter());
    this.changed(true);
  }

  stop() {
    if (this.probeTimer) clearTimeout(this.probeTimer);
    this.probeTimer = null;
    const a = this.adapter;
    this.adapter = null;
    if (a) a.disconnect('Disconnected by user');
  }

  private async diagnose(base: string, authorization: string | null) {
    if (this.s.connected || this.s.base !== base) return;
    let problem: string;
    let needsPassword = false;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 6000);   // a laptop that drops packets would otherwise hang this
    try {
      const r = await fetch(base + '/api/health', { signal: abort.signal,
        headers: { 'X-VibeGuard-Client': CLIENT, ...(authorization ? { Authorization: authorization } : {}) } });
      if (r.status === 401) {
        needsPassword = true;
        problem = authorization ? 'Wrong user name or password.' : 'This laptop asks for a user name and password.';
      } else if (r.status === 403) {
        problem = 'The laptop refused this phone. VibeGuard on the laptop needs to be started for phones (--host 0.0.0.0).';
      } else if (r.ok) {
        problem = 'The laptop answers, but the live connection failed. Is it the VibeGuard backend?';
      } else {
        problem = `The laptop answered with an error (HTTP ${r.status}).`;
      }
    } catch {
      problem = `Could not reach ${label(base)}. Is the laptop on, with VibeGuard running, on the same Wi-Fi as this phone?`;
    } finally {
      clearTimeout(timer);
    }
    if (this.s.connected || this.s.base !== base) return;
    this.s.problem = problem;
    this.s.needsPassword = needsPassword;
    if (needsPassword) this.stop();       // no point retrying without the password
    this.changed(true);
  }

  private adapterOnce(channel: string, fn: (m: any) => void) {
    let done = false;
    this.adapter?.on(channel, (m) => { if (!done) { done = true; fn(m); } });
  }

  private attach(a: Adapter) {
    this.adapter = a;
    const S = () => this.s;
    const mine = (fn: (m: any) => void) => (m: any) => { if (this.adapter === a) fn(m); };

    a.on('connection', mine((m) => {
      const s = S();
      s.connected = m.connected;
      if (m.connected) {
        s.everConnected = true;
        s.problem = null;
        s.needsPassword = false;
      } else {
        s.latest = null;
        const now = performance.now();
        for (const k of Object.keys(s.series) as (keyof State['series'])[]) s.series[k].push({ t: now, v: null });
        s.devices = { sleeve: disconnected(m.reason || ''), station: disconnected(m.reason || '') };
        s.session = null;
        if (s.st.view === 'running') {
          s.st = { ...s.st, view: 'idle', current: null, msg: `${m.reason || 'Connection lost'}. The test was stopped and nothing was saved.` };
        }
        if (s.cal.phase === 'relax' || s.cal.phase === 'squeeze') {
          s.cal = { ...s.cal, phase: 'failed', msg: `${m.reason || 'Connection lost'}. Calibration was not saved.` };
        }
        if (m.reason && m.reason !== 'Disconnected by user') this.toast(m.reason);
      }
      this.changed(true);
    }));

    a.on('state', mine((m) => {
      const s = S();
      s.devices = { ...s.devices, [m.device_role]: { state: m.state, detail: m.detail || '' } };
      if (m.device_role === 'sleeve' && m.state !== 'recording') s.session = null;
      this.changed(true);
    }));

    a.on('telemetry', mine((m: Telemetry) => {
      const s = S();
      const now = performance.now();
      if (s.lastTelemetryAt && now - s.lastTelemetryAt > STALE_MS) {
        for (const k of Object.keys(s.series) as (keyof State['series'])[]) s.series[k].push({ t: now - 1, v: null });
      }
      s.lastTelemetryAt = now;
      s.latest = m;
      const v = m.values;
      push(s.series.act, now, v.activation_pct ?? null, 30000);
      push(s.series.vib, now, v.vib_ahv_est ?? null, 30000);
      push(s.series.score, now, v.score_rate ?? null, 30000);
      push(s.series.raw, now, v.emg_env ?? null, 15000);
      this.changed();
    }));

    a.on('history', mine((m) => {
      const s = S();
      const known = new Set(s.saved.map((i) => (i.kind === 'session' ? i.rec.session_id : i.rec.test_id)));
      for (const rec of m.sessions as SessionRec[]) {
        if (!known.has(rec.session_id)) s.saved.push({ kind: 'session', at: rec.ended_at ?? '', rec });
      }
      for (const rec of (m.tests || []) as TestRec[]) {
        if (!known.has(rec.test_id)) s.saved.push({ kind: 'station', at: rec.completed_at ?? '', rec });
      }
      s.saved = [...s.saved].sort((x, y) => String(y.at).localeCompare(String(x.at)));   // newest first
      this.changed(true);
    }));

    a.on('session', mine((m: SessionRec) => {
      const s = S();
      s.saved = [{ kind: 'session', at: m.ended_at ?? new Date().toISOString(), rec: m }, ...s.saved];
      s.session = null;
      this.toast(m.ended_reason === 'stopped' ? 'Recording saved' : `Recording ended early and was saved: ${m.ended_reason}`);
    }));

    a.on('calibration', mine((m) => {
      const s = S();
      if (m.phase === 'done') {
        s.calibration = { rest: m.result.rest_env, max: m.result.max_env, at: m.result.calibrated_at };
        s.cal = { ...s.cal, phase: 'done' };
        s.recalibrate = false;
        this.toast('Calibration saved');
      } else if (m.phase === 'failed') {
        s.cal = { ...s.cal, phase: 'failed', msg: m.reason };
      } else if (m.phase === 'cancelled') {
        s.cal = { ...s.cal, phase: null, msg: '' };
      } else {
        s.cal = { ...s.cal, phase: m.phase, remaining: m.remaining_ms || 0, phaseMs: m.phase_ms || 3500 };
      }
      this.changed(true);
    }));

    a.on('tool', mine((m) => {
      S().tool = m;
      this.changed();
    }));

    a.on('event', mine((e) => {
      const s = S();
      if (e.event === 'boot_change' && !(e.detail && e.detail.previous_boot_id == null)) {
        s.recalibrate = true;
        s.calibration = null;
      }
      s.events = [{ ...e, text: eventText(e), at: e.at || new Date().toISOString(), warn: WARN_EVENTS.has(e.event) },
        ...s.events].slice(0, 30);
      this.changed(true);
    }));

    a.on('station', mine((m) => {
      const s = S();
      const st = { ...s.st };
      switch (m.type) {
        case 'station_started':
          Object.assign(st, { view: 'running', mode: m.mode, planned: m.planned_trials, trials: [], current: null, result: null, msg: '' });
          break;
        case 'station_trial':
          st.current = { ...m, receivedAt: performance.now(), answered: false };
          break;
        case 'station_trial_result':
          st.trials = [...st.trials, m];
          if (st.current && st.current.trial_id === m.trial_id) st.current = null;
          break;
        case 'station_complete':
          st.view = 'complete';
          st.current = null;
          st.result = m;
          st.trials = m.trials && m.trials.length ? m.trials : st.trials;
          s.saved = [{ kind: 'station', at: m.completed_at, rec: m }, ...s.saved];
          this.toast('Touch test saved');
          break;
        case 'station_aborted':
          st.view = 'idle';
          st.current = null;
          st.msg = `Test stopped: ${m.reason}. ${this.s.mode === 'backend' ? 'The laptop keeps it as an aborted test; it is not a result and is not listed in History.' : 'Nothing was saved.'}`;
          break;
      }
      s.st = st;
      this.changed(true);
    }));

    a.connect();
  }

  // ---- commands ----
  private async send(name: string, args?: Record<string, unknown>) {
    if (!this.adapter) return { status: 'error', error: 'Not connected to a laptop.' };
    return this.adapter.send(name, args);
  }

  async toggleRecording() {
    const rec = this.s.devices.sleeve.state === 'recording';
    this.s.busy = true;
    this.changed(true);
    const r = await this.send(rec ? 'stop_session' : 'start_session');
    this.s.busy = false;
    if (r.status === 'error') this.toast(String(r.error));
    else if (!rec) this.s.session = { id: String(r.session_id), startedAt: Date.now() };
    this.changed(true);
  }

  async calStep(command: 'calibrate_relaxed' | 'calibrate_squeeze' | 'calibrate_cancel') {
    this.s.cal = { ...this.s.cal, msg: '' };
    if (command === 'calibrate_relaxed') this.s.series.raw = [];
    const r = await this.send(command);
    if (r.status === 'error') this.s.cal = { ...this.s.cal, phase: 'failed', msg: String(r.error) };
    this.changed(true);
  }

  async newShift() {
    const r = await this.send('new_shift');
    this.toast(r.status === 'error' ? String(r.error) : 'New shift started: tool time reset');
  }

  async startTest(mode: 'preview' | 'full') {
    this.s.st = { ...blankStation(), mode };
    this.changed(true);
    const r = await this.send('start_station_test', { mode });
    if (r.status === 'error') { this.s.st = { ...this.s.st, msg: String(r.error) }; this.changed(true); }
  }

  async respond(response: 'felt' | 'not_felt') {
    const c = this.s.st.current;
    if (!c || c.answered) return;
    this.s.st = { ...this.s.st, current: { ...c, answered: true } };
    this.changed(true);
    const r = await this.send('submit_response', { trial_id: c.trial_id, response });
    if (r.status === 'error') this.toast(String(r.error));
  }

  async abortTest() {
    const r = await this.send('abort_station_test');
    if (r.status === 'error') this.toast(String(r.error));
  }

  testAgain() {
    this.s.st = blankStation();
    this.changed(true);
  }

  // practice feed controls
  practice = {
    squeeze: (on: boolean) => this.adapter?.setSqueeze?.(on),
    vibration: (on: boolean) => this.adapter?.setVibration?.(on),
    drop: () => this.adapter?.simulateDrop?.(),
    error: () => this.adapter?.injectError?.(),
  };
}

function push(series: Pt[], t: number, v: number | null, windowMs: number) {
  series.push({ t, v });
  const cutoff = t - windowMs - 1000;
  let drop = 0;
  while (drop < series.length && series[drop].t < cutoff) drop++;
  if (drop) series.splice(0, drop);
}

export const store = new Store();

/** Re-render when the store changes; returns the (mutable) state. */
export function useStore(): State {
  useSyncExternalStore(store.subscribe, store.getVersion);
  return store.s;
}

/** Fresh, stale (no reading for a second) or none. */
export function freshness(s: State, now = performance.now()): 'fresh' | 'stale' | 'none' {
  if (!s.connected || !s.latest) return 'none';
  return now - s.lastTelemetryAt > STALE_MS ? 'stale' : 'fresh';
}
