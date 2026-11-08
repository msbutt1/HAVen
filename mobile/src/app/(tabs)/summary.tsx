/**
 * Summary: KPI tiles and charts over the saved recordings and touch tests (the web dashboard's Overview). Changes are
 * neutral words, and no chart draws a safe or unsafe line: there are no validated limits behind these numbers.
 */
import { Pressable, Text, View } from 'react-native';
import { useState } from 'react';

import { Donut, Dots, Legend, Lines, RankedBars, StackedColumns, StackedRows } from '../../components/charts';
import { Screen } from '../../components/frame';
import { Eyebrow, ExperimentalTag, Muted, Panel, Stripes } from '../../components/ui';
import { useStore } from '../../state/store';
import { SOURCE_COLOR, useTheme } from '../../theme';
import { duration, num, score, when } from '../../vg/format.ts';
import { answers, change, perDay, slice, SOURCE_LABEL, SOURCES, stats, window, type Period, type SourceFilter } from '../../vg/summary.ts';

export default function Summary() {
  const t = useTheme();
  const s = useStore();
  const [period, setPeriod] = useState<Period>(30);
  const [source, setSource] = useState<SourceFilter>('all');
  const now = Date.now();
  const w = window(period, now, s.saved);
  const sl = slice(s.saved, w.from, w.to, source);
  const cur = stats(sl);
  const prev = w.previousFrom != null ? stats(slice(s.saved, w.previousFrom, w.from, source)) : null;
  const total = sl.sessions.length + sl.tests.length;
  const simulated = [...sl.sessions, ...sl.tests].filter((x) => x.provenance === 'simulated').length;
  const sc = SOURCE_COLOR(t);
  const compare = period === 0 ? null : `previous ${period} days`;

  const days = perDay(sl);
  const dayRows = Object.values(days.reduce<Record<number, { label: string; values: Record<string, number> }>>((a, d) => {
    a[d.day] = a[d.day] ?? { label: new Date(d.day).toLocaleDateString([], { month: 'short', day: 'numeric' }), values: {} };
    a[d.day].values[d.source] = d.count;
    return a;
  }, {}));
  const grip = sl.sessions.filter((x) => x.mean_activation_pct != null);
  const scored = [...sl.sessions].filter((x) => x.score_total != null).sort((a, b) => (b.score_total as number) - (a.score_total as number)).slice(0, 8);
  const toolRows = sl.sessions.filter((x) => x.tool_time_s != null);
  const toolBase = toolRows.reduce((a, x) => a + x.duration_s, 0);
  const tests = sl.tests.slice(-6);

  return (
    <Screen>
      <View style={{ gap: 4 }}>
        <Eyebrow>Summary</Eyebrow>
        <Text style={{ color: t.foreground, fontSize: 24, fontWeight: '700' }}>Recordings and tests</Text>
        <Muted size={16}>{total === 0 ? (s.connected ? 'Nothing saved in this period.' : 'Connect to the laptop to see saved recordings.')
          : `${cur.recordings} recording${cur.recordings === 1 ? '' : 's'} and ${cur.tests} touch test${cur.tests === 1 ? '' : 's'}. Same items as History.`}</Muted>
      </View>

      <Choice options={[[7, 'Last 7 days'], [30, 'Last 30 days'], [0, 'All time']] as [Period, string][]} value={period} onChange={setPeriod} />
      <Choice options={[['all', 'All'], ['live', 'Live'], ['replay', 'Replay'], ['simulated', 'Simulated']] as [SourceFilter, string][]} value={source} onChange={setSource} />

      {simulated > 0 && (
        <View style={{ borderRadius: 10, padding: 12, overflow: 'hidden' }}>
          <Stripes t={t} />
          <Text style={{ color: '#fff', fontSize: 15, fontWeight: '600' }}>
            {simulated === total ? 'All of this is simulated data, not measurements from a person.' : `${simulated} of ${total} items here are simulated, not measurements from a person.`}
          </Text>
        </View>
      )}

      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10 }}>
        <Kpi title="Recordings" value={String(cur.recordings)} change={prev && change(cur.recordings, prev.recordings)} was={prev && String(prev.recordings)} compare={compare} />
        <Kpi title="Recorded time" value={duration(cur.recordedS)} change={prev && change(cur.recordedS, prev.recordedS)} was={prev && duration(prev.recordedS)} compare={compare} />
        <Kpi title="Tool time (estimated)" value={duration(cur.toolS)} change={prev && change(cur.toolS, prev.toolS)} was={prev && duration(prev.toolS)} compare={compare}
          extra={cur.toolShare != null ? `${Math.round(cur.toolShare * 100)} % of recorded time` : undefined} />
        <Kpi title="Highest peak vibration" value={cur.peakVibration == null ? '—' : `${num(cur.peakVibration, 1)} m/s²`} change={prev && change(cur.peakVibration, prev.peakVibration)}
          was={prev && (prev.peakVibration == null ? '—' : `${num(prev.peakVibration, 1)} m/s²`)} compare={compare} extra="unweighted" />
        <Kpi title="Mean grip effort" value={cur.meanGrip == null ? '—' : `${num(cur.meanGrip, 0)} %`} change={prev && change(cur.meanGrip, prev.meanGrip)}
          was={prev && (prev.meanGrip == null ? '—' : `${num(prev.meanGrip, 0)} %`)} compare={compare} extra="of each wearer’s calibration" />
        <Kpi title="Touch tests" value={String(cur.tests)} change={prev && change(cur.tests, prev.tests)} was={prev && String(prev.tests)} compare={compare}
          extra={cur.lastThreshold != null ? `latest threshold: ${num(cur.lastThreshold, 0)}` : undefined} />
      </View>

      <Card title="Recordings per day">
        {dayRows.length ? (
          <>
            <StackedColumns days={dayRows} keys={SOURCES} colors={sc} />
            {new Set(days.map((d) => d.source)).size > 1 && <Legend items={SOURCES.filter((k) => days.some((d) => d.source === k)).map((k) => ({ label: SOURCE_LABEL[k], color: sc[k] }))} />}
          </>
        ) : <Muted>No recordings in this period.</Muted>}
      </Card>

      <Card title="Grip effort per recording" subtitle="mean and peak, % of calibration">
        {grip.length ? (
          <>
            <Lines minMax={100} suffix=" %" series={[
              { label: 'Mean', color: t.ovBlue, values: grip.map((x) => x.mean_activation_pct ?? null) },
              { label: 'Peak', color: t.ovBlue2, values: grip.map((x) => x.peak_activation_pct ?? null) }]} />
            <Legend items={[{ label: 'Mean', color: t.ovBlue }, { label: 'Peak', color: t.ovBlue2 }]} />
          </>
        ) : <Muted>No calibrated recordings in this period (grip effort needs a calibration).</Muted>}
      </Card>

      <Card title="Highest exposure scores" experimental>
        {scored.length ? <RankedBars color={t.ovTeal} format={(v) => `${score(v)} au`} rows={scored.map((x) => ({ label: when(x.ended_at), value: x.score_total as number }))} />
          : <Muted>No scored recordings in this period (the score needs a calibration).</Muted>}
      </Card>

      <Card title="Recording time with a tool" subtitle="estimated from vibration">
        {toolBase > 0 ? (
          <>
            <Donut parts={[{ value: Math.min(toolBase, cur.toolS ?? 0), color: t.ovOrange }, { value: Math.max(0, toolBase - (cur.toolS ?? 0)), color: t.ovTrack }]}
              center={<View style={{ alignItems: 'center' }}><Text style={{ color: t.foreground, fontSize: 30, fontWeight: '700' }}>{Math.round((cur.toolShare ?? 0) * 100)} %</Text><Muted size={13}>of recorded time</Muted></View>} />
            <Legend items={[{ label: 'Tool running (estimated)', color: t.ovOrange }, { label: 'No tool detected', color: t.ovTrack }]} />
          </>
        ) : <Muted>No tool-use data in this period.</Muted>}
      </Card>

      <Card title="Touch test answers" subtitle="newest tests">
        {tests.length ? (
          <>
            <StackedRows keys={['felt', 'notFelt', 'missing']} colors={{ felt: t.ovBlue, notFelt: t.ovOrange, missing: t.ovNeutral }}
              rows={tests.map((x) => ({ label: when(x.completed_at), values: answers(x) as unknown as Record<string, number> }))} />
            <Legend items={[{ label: 'Felt it', color: t.ovBlue }, { label: 'Didn’t feel it', color: t.ovOrange }, { label: 'No answer', color: t.ovNeutral }]} />
          </>
        ) : <Muted>No touch tests in this period.</Muted>}
      </Card>

      <Card title="Grip effort vs peak vibration" subtitle="each dot is a recording">
        {sl.sessions.some((x) => x.peak_vib_ahv_est != null && x.mean_activation_pct != null)
          ? <Dots xLabel="Peak vibration (m/s²)" yLabel="Mean grip effort (%)" points={sl.sessions.filter((x) => x.peak_vib_ahv_est != null && x.mean_activation_pct != null)
            .map((x) => ({ x: x.peak_vib_ahv_est as number, y: x.mean_activation_pct as number, color: sc[x.provenance] }))} />
          : <Muted>No recordings with both grip effort and vibration in this period.</Muted>}
      </Card>

      <Card title="Peak vibration per recording" subtitle="m/s², unweighted">
        {sl.sessions.some((x) => x.peak_vib_ahv_est != null)
          ? <Lines series={[{ label: 'Peak', color: t.ovOrange, values: sl.sessions.map((x) => x.peak_vib_ahv_est ?? null) }]} />
          : <Muted>No vibration readings in this period.</Muted>}
      </Card>

      <Card title="Touch test threshold" subtitle="motor setting, higher means less sensitive">
        {sl.tests.some((x) => x.threshold_setting != null)
          ? <Lines series={[{ label: 'Threshold', color: t.ovBlue, values: sl.tests.filter((x) => x.threshold_setting != null).map((x) => x.threshold_setting) }]} />
          : <Muted>No touch test thresholds in this period.</Muted>}
      </Card>

      <Muted size={14}>Measured vibration is unweighted (not ISO 5349). Grip effort is relative to each wearer’s calibration, not force. The exposure score is experimental and not a dose. No chart draws a safe or unsafe line: there are no validated limits behind these numbers.</Muted>
    </Screen>
  );
}

function Choice<T extends string | number>({ options, value, onChange }: { options: [T, string][]; value: T; onChange: (v: T) => void }) {
  const t = useTheme();
  return (
    <View style={{ flexDirection: 'row', borderRadius: 12, borderWidth: 1, borderColor: t.border, padding: 3, backgroundColor: t.section }}>
      {options.map(([v, l]) => (
        <Pressable key={String(v)} accessibilityRole="button" accessibilityState={{ selected: v === value }} onPress={() => onChange(v)}
          style={{ flex: 1, minHeight: 44, borderRadius: 9, alignItems: 'center', justifyContent: 'center', backgroundColor: v === value ? t.card : 'transparent',
            borderWidth: v === value ? 1 : 0, borderColor: t.border }}>
          <Text style={{ color: v === value ? t.foreground : t.muted, fontSize: 15, fontWeight: '600' }}>{l}</Text>
        </Pressable>
      ))}
    </View>
  );
}

function Kpi({ title, value, change: ch, was, compare, extra }: { title: string; value: string; change: string | null; was: string | null; compare: string | null; extra?: string }) {
  const t = useTheme();
  return (
    <Panel style={{ width: '48.5%', alignItems: 'center', gap: 4, padding: 12 }}>
      <Text style={{ color: t.muted, fontSize: 14, textAlign: 'center' }}>{title}</Text>
      <Text adjustsFontSizeToFit numberOfLines={1} style={{ color: t.foreground, fontSize: 28, fontWeight: '700' }}>{value}</Text>
      {ch != null && compare && (
        <>
          <Text style={{ color: t.foreground, fontSize: 14, fontWeight: '700' }}>{ch}</Text>
          <Text style={{ color: t.muted, fontSize: 12, textAlign: 'center' }}>was {was}, {compare}</Text>
        </>
      )}
      {extra && <Text style={{ color: t.muted, fontSize: 12, textAlign: 'center' }}>{extra}</Text>}
    </Panel>
  );
}

function Card({ title, subtitle, experimental, children }: { title: string; subtitle?: string; experimental?: boolean; children: React.ReactNode }) {
  const t = useTheme();
  return (
    <Panel>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Text style={{ color: t.foreground, fontSize: 17, fontWeight: '700' }}>{title}</Text>
        {experimental && <ExperimentalTag />}
      </View>
      {subtitle && <Muted size={13}>{subtitle}</Muted>}
      {children}
    </Panel>
  );
}
