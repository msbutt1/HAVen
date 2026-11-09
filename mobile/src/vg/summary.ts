/**
 * The Summary tab's numbers, from the saved history (the same items as the History tab), with the web dashboard's
 * Overview rules (dashboard/js/overview.js; ios/HAVen/Summary.swift). Pure: no React Native imports, so
 * `node --test` runs it. No safe/unsafe line and no good/bad colour: there are no validated limits behind these.
 */

export type Source = 'live' | 'replay' | 'simulated';
export const SOURCES: Source[] = ['live', 'replay', 'simulated'];
export const SOURCE_LABEL: Record<Source, string> = { live: 'Live', replay: 'Replay', simulated: 'Simulated' };

/** A saved recording, as the shared adapter reports it ("session_saved"). */
export interface SessionRec {
  session_id: string;
  provenance: Source;
  started_at?: string | null;
  ended_at?: string | null;
  duration_s: number;
  ended_reason: string;
  calibrated: boolean;
  score_total: number | null;
  score_units?: string;
  mean_activation_pct?: number | null;
  peak_activation_pct?: number | null;
  peak_vib_ahv_est?: number | null;
  tool_time_s?: number | null;
}

/** A saved touch test ("station_complete"). */
export interface TestRec {
  test_id: string;
  provenance: Source;
  mode: 'preview' | 'full';
  completed_at?: string | null;
  threshold_setting: number | null;
  missing_responses?: number;
  trials: { response: string }[];
}

export type Saved = { kind: 'session'; at: string; rec: SessionRec } | { kind: 'station'; at: string; rec: TestRec };

export type Period = 7 | 30 | 0; // 0: all time
export type SourceFilter = 'all' | Source;

export interface Slice { sessions: (SessionRec & { t: number })[]; tests: (TestRec & { t: number })[] }

export interface Stats {
  recordings: number;
  recordedS: number;
  toolS: number | null; // null when no recording measured tool use
  toolShare: number | null; // of the recorded time of the recordings that measured it
  peakVibration: number | null;
  meanGrip: number | null; // missing grip is left out, never counted as 0
  tests: number;
  lastThreshold: number | null;
}

const DAY = 86400000;
const startOfDay = (t: number) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** Whole calendar days ending today: "Last 7 days" is today and the 6 before; the previous period is just before. */
export function window(period: Period, now: number, items: Saved[]): { from: number; to: number; previousFrom: number | null } {
  const to = startOfDay(now) + DAY;
  if (period === 0) {
    const times = items.map((i) => Date.parse(i.at)).filter(Number.isFinite);
    return { from: startOfDay(Math.min(now, ...times)), to, previousFrom: null };
  }
  const from = to - period * DAY;
  return { from, to, previousFrom: from - period * DAY };
}

export function slice(items: Saved[], from: number, to: number, source: SourceFilter): Slice {
  const out: Slice = { sessions: [], tests: [] };
  for (const it of items) {
    const t = Date.parse(it.at);
    if (!Number.isFinite(t) || t < from || t >= to) continue;
    if (source !== 'all' && it.rec.provenance !== source) continue;
    if (it.kind === 'session') out.sessions.push({ ...it.rec, t });
    else out.tests.push({ ...it.rec, t });
  }
  out.sessions.sort((a, b) => a.t - b.t);
  out.tests.sort((a, b) => a.t - b.t);
  return out;
}

export function stats(s: Slice): Stats {
  const withTool = s.sessions.filter((x) => isNum(x.tool_time_s));
  const toolS = withTool.reduce((a, x) => a + (x.tool_time_s as number), 0);
  const toolBase = withTool.reduce((a, x) => a + (x.duration_s || 0), 0);
  const vibs = s.sessions.map((x) => x.peak_vib_ahv_est).filter(isNum);
  const grips = s.sessions.map((x) => x.mean_activation_pct).filter(isNum);
  const last = [...s.tests].reverse().find((x) => isNum(x.threshold_setting));
  return {
    recordings: s.sessions.length,
    recordedS: s.sessions.reduce((a, x) => a + (x.duration_s || 0), 0),
    toolS: withTool.length ? toolS : null,
    toolShare: toolBase > 0 ? toolS / toolBase : null,
    peakVibration: vibs.length ? Math.max(...vibs) : null,
    meanGrip: grips.length ? grips.reduce((a, b) => a + b, 0) / grips.length : null,
    tests: s.tests.length,
    lastThreshold: last ? last.threshold_setting : null,
  };
}

/** "▲ 12 %", "▼ 3 %", "± 0 %", "new" (nothing before) or "—" (no number to compare). Neutral words, no colour. */
export function change(current: number | null, previous: number | null): string {
  if (!isNum(current) || !isNum(previous)) return '—';
  if (previous === 0) return current === 0 ? '± 0 %' : 'new';
  const pct = ((current - previous) / Math.abs(previous)) * 100;
  if (Math.abs(pct) < 0.5) return '± 0 %';
  return `${pct > 0 ? '▲' : '▼'} ${Math.round(Math.abs(pct))} %`;
}

/** Recordings per calendar day and source (days with none left out), oldest first. */
export function perDay(s: Slice): { day: number; source: Source; count: number }[] {
  const m = new Map<string, { day: number; source: Source; count: number }>();
  for (const r of s.sessions) {
    const day = startOfDay(r.t);
    const k = `${day}|${r.provenance}`;
    const e = m.get(k) ?? { day, source: r.provenance, count: 0 };
    e.count += 1;
    m.set(k, e);
  }
  return [...m.values()].sort((a, b) => a.day - b.day || SOURCES.indexOf(a.source) - SOURCES.indexOf(b.source));
}

/** Felt / didn't feel / no answer counts of one test. */
export function answers(t: TestRec): { felt: number; notFelt: number; missing: number } {
  const n = (r: string) => t.trials.filter((x) => x.response === r).length;
  return { felt: n('felt'), notFelt: n('not_felt'), missing: n('missing') };
}
