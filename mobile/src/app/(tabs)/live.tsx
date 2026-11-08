/** Live: recording first, then grip effort, wrist vibration, the experimental score, tool use, messages, charts. */
import { Ionicons } from '@expo/vector-icons';
import { router } from 'expo-router';
import { useKeepAwake } from 'expo-keep-awake';
import { useState } from 'react';
import { Alert, Pressable, Switch, Text, View } from 'react-native';

import { LiveLine } from '../../components/charts';
import { Screen } from '../../components/frame';
import { useTick } from '../../components/tick';
import { BigButton, ExperimentalTag, Muted, Note, Panel } from '../../components/ui';
import { freshness, store, useStore, type State } from '../../state/store';
import { useTheme } from '../../theme';
import { clock, num, score, timeOfDay } from '../../vg/format.ts';

const TOOL_STATE: Record<string, string> = { tool: 'Tool in use', no_tool: 'No tool', unknown: '—' };
const WARNING_TEXT: Record<string, string> = {
  continuous_tool_use_long: 'Long continuous tool use. Consider a break.',
  session_tool_time_high: 'Tool time this shift is high.',
  session_tool_time_approaching: 'Tool time this shift is getting high.',
};
const secs = (s: number | null | undefined) => (s == null ? '—' : s < 60 ? `${Math.round(s)} s`
  : `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, '0')} s`);

export default function Live() {
  useKeepAwake();                      // a live monitor: the screen stays on while it is open
  const t = useTheme();
  const s = useStore();
  const now = useTick();
  const f = freshness(s, now);
  const v = f === 'fresh' ? s.latest?.values ?? null : null;
  const q = (f === 'fresh' && s.latest?.quality) || {};
  const calibrated = !!(s.calibration || s.latest?.quality?.calibrated);

  const gripNote = !calibrated
    ? (s.recalibrate ? 'The wearable restarted, so the calibration was dropped. Calibrate again.' : 'Not calibrated yet. Calibrate to see grip effort.')
    : [s.calibration ? `Calibrated at ${timeOfDay(s.calibration.at)}.` : 'Calibrated before this screen opened.',
      q.above_max ? 'Above your calibrated maximum.' : '', q.below_rest ? 'Below your relaxed baseline.' : '',
      q.degraded ? 'Some samples clipped: treat with caution.' : '', 'Muscle activity, not grip force.'].filter(Boolean).join(' ');

  return (
    <Screen>
      <Text style={{ color: t.muted, fontSize: 15 }}>
        {f === 'fresh' ? `Updating ${s.latest?.sample_rate_hz ?? 20} times a second.` : f === 'stale' ? 'No new reading. Values are hidden.' : s.connected ? 'Waiting for the first reading.' : 'No data.'}
      </Text>

      <RecordingCard s={s} />

      <Panel>
        <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>Grip effort</Text>
        <Big value={num(v?.activation_pct != null ? Math.max(0, v.activation_pct) : null, 0)} unit="% of your strongest squeeze" stale={f !== 'fresh'} />
        <View style={{ height: 12, borderRadius: 999, backgroundColor: t.section, overflow: 'hidden' }}>
          <View style={{ height: '100%', width: `${Math.min(100, Math.max(0, v?.activation_bar_pct ?? v?.activation_pct ?? 0))}%`, backgroundColor: t.activation }} />
        </View>
        {!calibrated && s.connected && s.devices.sleeve.state !== 'recording' && (
          <BigButton title="Calibrate now" onPress={() => router.navigate('/calibrate')} />
        )}
        <Muted>{gripNote}</Muted>
      </Panel>

      {q.emg_vibration_artifact && <Note text="Tool vibration may be leaking into the muscle signal. Treat grip effort and score with caution." />}

      <Panel>
        <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>Wrist vibration</Text>
        <Big value={num(v?.vib_ahv_est, 2)} unit="m/s²" stale={f !== 'fresh'} />
        <Muted>How strongly the wrist is shaking. An estimate from a hobby motion sensor (unweighted), not a certified measurement.</Muted>
      </Panel>

      <Panel dashed>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
          <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>Exposure score</Text>
          <ExperimentalTag />
        </View>
        <Big value={num(v?.score_rate, 2)} unit={s.latest?.units?.score_rate === 'pts/min' ? 'points / min' : (s.latest?.units?.score_rate ?? 'au/h')} stale={f !== 'fresh'} />
        <Text style={{ color: t.foreground, fontSize: 16 }}>This recording: <Text style={{ fontWeight: '700' }}>{score(v?.score_total)}</Text></Text>
        <Muted>A test feature that weights vibration by grip. It is not a safety limit or a dose.</Muted>
      </Panel>

      <ToolCard s={s} fresh={f === 'fresh'} />

      <Panel>
        <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>Messages</Text>
        {s.events.length === 0 ? <Muted>Nothing yet.</Muted> : s.events.slice(0, 8).map((e, i) => (
          <View key={i} style={{ flexDirection: 'row', gap: 10 }}>
            <Text style={{ color: t.muted, fontSize: 13, width: 78 }}>{timeOfDay(e.at)}</Text>
            <Text style={{ color: e.warn ? t.destructive : t.foreground, fontSize: 15, flex: 1 }}>{e.text}</Text>
          </View>
        ))}
      </Panel>

      <Panel>
        <Muted size={14}>Grip effort, last 30 s</Muted>
        <LiveLine series={s.series.act} now={now} color={t.activation} fixedMax={100} stale={f !== 'fresh'} />
        <Muted size={14}>Wrist vibration, last 30 s</Muted>
        <LiveLine series={s.series.vib} now={now} color={t.vibration} stale={f !== 'fresh'} />
        <Muted size={14}>Exposure score rate (experimental), last 30 s</Muted>
        <LiveLine series={s.series.score} now={now} color={t.score} stale={f !== 'fresh'} />
      </Panel>

      {s.mode === 'demo' && <PracticeControls />}
    </Screen>
  );
}

function Big({ value, unit, stale }: { value: string; unit: string; stale: boolean }) {
  const t = useTheme();
  return (
    <View style={{ flexDirection: 'row', alignItems: 'baseline', flexWrap: 'wrap', gap: 8 }}>
      <Text accessibilityLabel={`${value} ${unit}`} style={{ color: stale ? t.muted : t.foreground, fontSize: 54, fontWeight: '700',
        fontVariant: ['tabular-nums'], letterSpacing: -1.5 }}>{value}</Text>
      <Text style={{ color: t.muted, fontSize: 16 }}>{unit}</Text>
    </View>
  );
}

function RecordingCard({ s }: { s: State }) {
  const t = useTheme();
  const st = s.devices.sleeve.state;
  const rec = st === 'recording';
  const calibrated = !!(s.calibration || s.latest?.quality?.calibrated);
  const msg = rec ? (s.session ? `Recording: ${clock(Date.now() - s.session.startedAt)}` : 'Recording')
    : st === 'ready' ? (calibrated ? 'Ready to record.' : 'Ready to record. Not calibrated, so the score will stay empty.')
    : st === 'calibrating' ? 'Wait for calibration to finish.'
    : st === 'error' ? 'Clear the sleeve error before recording.'
    : 'Connect the sleeve to record.';
  return (
    <Panel style={{ alignItems: 'stretch' }}>
      <BigButton title={rec ? 'Stop recording' : 'Start recording'} kind={rec ? 'danger' : 'primary'} height={72}
        icon={<Ionicons name={rec ? 'stop' : 'radio-button-on'} size={24} color={rec ? '#fff' : t.onPrimary} />}
        busy={s.busy} disabled={!(st === 'ready' || rec)} onPress={() => store.toggleRecording()} />
      <Text style={{ color: t.muted, fontSize: 16, textAlign: 'center', fontVariant: ['tabular-nums'] }}>{msg}</Text>
    </Panel>
  );
}

function ToolCard({ s, fresh }: { s: State; fresh: boolean }) {
  const t = useTheme();
  const tool = s.tool;
  const live = !!tool && !tool.saved && s.connected;
  const fact = (k: string, v: string) => (
    <View style={{ width: '50%', paddingVertical: 4 }}>
      <Text style={{ color: t.muted, fontSize: 14 }}>{k}</Text>
      <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700', fontVariant: ['tabular-nums'] }}>{v}</Text>
    </View>
  );
  return (
    <Panel>
      <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>Tool use</Text>
      <Text style={{ color: t.foreground, fontSize: 34, fontWeight: '700' }}>{live && fresh && tool.status === 'ok' ? (TOOL_STATE[tool.state] ?? '—') : '—'}</Text>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap' }}>
        {fact('Tool time this shift', tool ? secs(tool.session_tool_s) + (tool.saved ? ' (last known)' : '') : '—')}
        {fact('Tool bouts', tool?.bouts != null ? String(tool.bouts) : '—')}
        {fact('Longest without a break', tool ? secs(tool.longest_bout_s) : '—')}
        {fact('Tool frequency', live && tool.dominant_hz != null ? `${Math.round(tool.dominant_hz)} Hz` : '—')}
      </View>
      {(live ? tool.warnings || [] : []).map((w: string) => <Note key={w} text={WARNING_TEXT[w] ?? w.replace(/_/g, ' ')} />)}
      <Muted>Estimated from vibration. Warnings are provisional team guidance (tuned on a phone motor), not legal limits.</Muted>
      <BigButton title="Start a new shift" kind="outline" disabled={!s.connected} onPress={() =>
        Alert.alert('Start a new shift?', 'Tool time and bouts go back to zero.', [
          { text: 'Cancel', style: 'cancel' },
          { text: 'Start new shift', style: 'destructive', onPress: () => store.newShift() },
        ])} />
    </Panel>
  );
}

/** Practice mode only: drive the made-up sleeve. */
function PracticeControls() {
  const t = useTheme();
  const [tool, setTool] = useState(false);
  return (
    <View style={{ borderRadius: 16, padding: 16, gap: 12, borderWidth: 1.5, borderStyle: 'dashed', borderColor: t.sim }}>
      <Text style={{ color: t.sim, fontSize: 14, fontWeight: '700', letterSpacing: 1.5 }}>PRACTICE CONTROLS</Text>
      <Pressable accessibilityRole="button" onPressIn={() => store.practice.squeeze(true)} onPressOut={() => store.practice.squeeze(false)}
        style={({ pressed }) => ({ minHeight: 60, borderRadius: 12, alignItems: 'center', justifyContent: 'center',
          backgroundColor: pressed ? t.primary : t.section, borderWidth: 1, borderColor: t.border })}>
        {({ pressed }) => <Text style={{ fontSize: 18, fontWeight: '600', color: pressed ? t.onPrimary : t.foreground }}>Hold to squeeze</Text>}
      </Pressable>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', minHeight: 48 }}>
        <Text style={{ color: t.foreground, fontSize: 17 }}>Tool running</Text>
        <Switch value={tool} onValueChange={(on) => { setTool(on); store.practice.vibration(on); }} />
      </View>
      <BigButton title="Drop the connection" kind="outline" onPress={() => store.practice.drop()} />
      <BigButton title="Make a sleeve error" kind="outline" onPress={() => store.practice.error()} />
    </View>
  );
}
