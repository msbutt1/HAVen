// Run by test_dashboard_overview.py: loads the dashboard's mock-adapter.js (for the sample history) and
// overview.js with a minimal fake page, and prints what the Overview computes from saved items.
const fs = require('fs'), vm = require('vm'), path = require('path');
const dash = path.join(__dirname, '..', '..', 'dashboard', 'js');
const stub = () => ({ addEventListener() {}, value: '30', hidden: false });
const ctx = {
  window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
  document: { getElementById: stub, createElement: () => ({ getContext: () => ({}) }) },
  performance: { now: () => 0 }, setTimeout, clearTimeout, setInterval, clearInterval, console,
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
for (const f of ['contract.js', 'mock-adapter.js', 'overview.js']) vm.runInContext(fs.readFileSync(path.join(dash, f), 'utf8'), ctx, { filename: f });
const VG = ctx.window.VG;
const out = {};

// sample history: deterministic, all simulated and marked sample, nothing after "now"
const now = Date.parse('2026-11-08T18:00:00Z');
const h1 = VG.sampleHistory(now), h2 = VG.sampleHistory(now);
const all = [...h1.sessions, ...h1.tests];
out.sample = {
  sessions: h1.sessions.length, tests: h1.tests.length,
  same: JSON.stringify(h1) === JSON.stringify(h2),
  allSimulated: all.every((r) => r.provenance === 'simulated' && r.sample === true),
  noFuture: all.every((r) => Date.parse(r.ended_at || r.completed_at) <= now),
  peakCapped: h1.sessions.every((s) => s.peak_activation_pct == null || s.peak_activation_pct <= 100),
  toolWithin: h1.sessions.every((s) => s.tool_time_s >= 0 && s.tool_time_s <= s.duration_s),
};

// the Overview's sums over a hand-made set of saved items (newest first, as the app keeps them)
const items = [
  { kind: 'station', at: '2026-11-07T15:00:00Z', rec: { test_id: 't2', provenance: 'live', threshold_setting: 80,
    trials: [{ response: 'felt' }, { response: 'felt' }, { response: 'not_felt' }, { response: 'missing' }] } },
  { kind: 'session', at: '2026-11-07T10:00:00Z', rec: { session_id: 's2', provenance: 'live', duration_s: 600, score_total: 2,
    mean_activation_pct: 50, peak_activation_pct: 90, peak_vib_ahv_est: 6, tool_time_s: 300 } },
  { kind: 'session', at: '2026-11-06T10:00:00Z', rec: { session_id: 's1', provenance: 'replay', duration_s: 1200, score_total: null,
    mean_activation_pct: null, peak_vib_ahv_est: 4, tool_time_s: null } },
  { kind: 'station', at: '2026-11-05T15:00:00Z', rec: { test_id: 't1', provenance: 'simulated', threshold_setting: null, trials: [] } },
  { kind: 'session', at: 'not a date', rec: { session_id: 'bad', provenance: 'live', duration_s: 5 } },
];
const d = VG.Overview._prepare(items);
out.order = [d.sessions.map((x) => x.src), d.tests.map((x) => [x.src, x.felt, x.notFelt, x.missing])];
out.stats = VG.Overview._stats(d);
out.liveOnly = VG.Overview._stats({ sessions: d.sessions.filter((x) => x.src === 'live'), tests: d.tests.filter((x) => x.src === 'live') });
console.log(JSON.stringify(out));
