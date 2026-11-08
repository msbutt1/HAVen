// Run with `npm test` (node --test; Node 24 runs TypeScript directly). Pure logic only: no React Native here.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { base64, baseURL, basicAuth, label } from './address.ts';
import { duration, score, when } from './format.ts';
import { answers, change, perDay, slice, stats, window, type Saved } from './summary.ts';

test('typed and scanned addresses become base URLs', () => {
  assert.equal(baseURL('192.168.1.23'), 'http://192.168.1.23:8000');
  assert.equal(baseURL(' 192.168.1.23:8010 '), 'http://192.168.1.23:8010');
  assert.equal(baseURL('http://172.28.88.153:8000/dashboard/'), 'http://172.28.88.153:8000'); // the laptop's QR code
  assert.equal(baseURL('http://Laptop.local:8000/dashboard/#live'), 'http://laptop.local:8000');
  assert.equal(baseURL('https://vg.example.org/dashboard/'), 'https://vg.example.org');
  for (const bad of ['', '   ', '192.168.1.23 8000', 'ftp://192.168.1.23', 'http://', '192.168.1.23:99999', 'WIFI:S:x;;'])
    assert.equal(baseURL(bad), null, bad);
  assert.equal(label('http://192.168.1.23:8000'), '192.168.1.23:8000');
});

test('the password header is standard base64', () => {
  assert.equal(basicAuth('team', 'pw'), 'Basic ' + Buffer.from('team:pw').toString('base64'));
  assert.equal(basicAuth('', ''), null);
  for (const s of ['a', 'ab', 'abc', 'abcd', 'pässwörd ✓'])
    assert.equal(base64(new TextEncoder().encode(s)), Buffer.from(s).toString('base64'));
});

test('formatting', () => {
  assert.equal(duration(45), '45 s');
  assert.equal(duration(2880), '48 min');
  assert.equal(duration(3840), '1 h 04 min');
  assert.equal(duration(null), '—');
  assert.equal(score(0.059868), '0.060');
  assert.equal(score(330.94), '330.9');
  const now = new Date(2026, 10, 8, 18, 0);
  assert.match(when(new Date(2026, 10, 8, 9, 0), now), /^Today, /);
  assert.match(when(new Date(2026, 10, 7, 9, 0), now), /^Yesterday, /);
  assert.equal(when('not a date', now), '—');
});

const now = new Date(2026, 10, 8, 18, 0).getTime();
const at = (daysAgo: number, hour = 10) => new Date(2026, 10, 8 - daysAgo, hour).toISOString();
const session = (id: string, a: string, extra: Partial<Saved['rec']> = {}): Saved => ({
  kind: 'session', at: a,
  rec: { session_id: id, provenance: 'live', ended_at: a, duration_s: 600, ended_reason: 'stopped', calibrated: true,
    score_total: 2, mean_activation_pct: 50, peak_activation_pct: 90, peak_vib_ahv_est: 4, tool_time_s: 300, ...extra } as any,
});
const testRec = (id: string, a: string, threshold: number | null, responses: string[]): Saved => ({
  kind: 'station', at: a,
  rec: { test_id: id, provenance: 'simulated', mode: 'preview', completed_at: a, threshold_setting: threshold,
    trials: responses.map((response) => ({ response })) },
});

test('summary: whole days, missing values left out, tool share only where measured', () => {
  const items = [session('a', at(0)),
    session('b', at(1), { provenance: 'replay', duration_s: 1200, mean_activation_pct: null, peak_activation_pct: null, peak_vib_ahv_est: 6, tool_time_s: null } as any),
    testRec('t1', at(2), 80, ['felt', 'felt', 'not_felt', 'missing']), testRec('t0', at(3), null, [])];
  const w = window(7, now, items);
  assert.equal(new Date(w.to).getDate(), 9);
  assert.equal(new Date(w.from).getDate(), 2); // today and the 6 days before
  assert.equal(w.previousFrom, w.from - 7 * 86400000);
  const s = slice(items, w.from, w.to, 'all');
  assert.deepEqual(s.sessions.map((x) => x.session_id), ['b', 'a']); // oldest first
  const st = stats(s);
  assert.equal(st.recordings, 2);
  assert.equal(st.recordedS, 1800);
  assert.equal(st.toolS, 300);
  assert.equal(st.toolShare, 0.5); // of the 600 s that measured it, not of 1800 s
  assert.equal(st.peakVibration, 6);
  assert.equal(st.meanGrip, 50);
  assert.equal(st.tests, 2);
  assert.equal(st.lastThreshold, 80);
  assert.deepEqual(answers(s.tests[1]), { felt: 2, notFelt: 1, missing: 1 });
  assert.equal(stats(slice(items, w.from, w.to, 'live')).recordings, 1);
  assert.equal(window(0, now, items).previousFrom, null);
});

test('summary: changes in neutral words, per-day counts by source', () => {
  assert.equal(change(12, 10), '▲ 20 %');
  assert.equal(change(9, 10), '▼ 10 %');
  assert.equal(change(10, 10), '± 0 %');
  assert.equal(change(3, 0), 'new');
  assert.equal(change(null, 4), '—');
  const items = [session('a', at(0, 9)), session('b', at(0, 15)), session('c', at(0, 16), { provenance: 'simulated' } as any), session('d', at(2))];
  const w = window(7, now, items);
  assert.deepEqual(perDay(slice(items, w.from, w.to, 'all')).map((d) => `${d.source}:${d.count}`), ['live:1', 'live:2', 'simulated:1']);
});
