/** Calibrate: relax, then squeeze, each with its own button and countdown (the guide's two steps). */
import { Text, View } from 'react-native';

import { LiveLine } from '../../components/charts';
import { Screen } from '../../components/frame';
import { useTick } from '../../components/tick';
import { BigButton, Muted, Panel } from '../../components/ui';
import { store, useStore } from '../../state/store';
import { useTheme } from '../../theme';
import { num, timeOfDay } from '../../vg/format.ts';

const PROMPTS: Record<string, string> = {
  relax: 'Relax your forearm completely, resting on a table. Hold still.',
  relaxed_captured: 'Relaxed level captured. Now squeeze as hard as you can and hold, then press Start squeeze.',
  squeeze: 'Squeeze as hard as you can and hold it.',
  done: 'Calibrated. Grip effort now shows as a percentage of that squeeze.',
};

export default function Calibrate() {
  const t = useTheme();
  const s = useStore();
  const now = useTick();
  const p = s.cal.phase;
  const running = p === 'relax' || p === 'squeeze';
  const st = s.devices.sleeve.state;
  const ready = st === 'ready';
  const prompt = p === 'failed' ? (s.cal.msg || 'Calibration failed. Start again with the relaxed step.')
    : (p && PROMPTS[p]) || (s.recalibrate ? 'The wearable restarted, so the calibration was dropped. Rest your arm on the table, then start again.'
      : 'Put the sleeve on, rest your arm on the table, then press Start relaxed.');
  const step = p === 'relax' ? 0 : (p === 'relaxed_captured' || p === 'squeeze') ? 1 : p === 'done' ? 2 : -1;
  const hint = st === 'disconnected' ? 'Connect the sleeve first.' : st === 'recording' ? 'Stop recording before calibrating.'
    : st === 'error' ? 'Clear the sleeve error first.' : s.mode === 'demo' && p === 'relaxed_captured' ? 'The practice sleeve squeezes by itself during this step.' : '';

  return (
    <Screen>
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {['Relaxed', 'Squeeze', 'Calibrated'].map((name, i) => {
          const active = i === step && p !== 'done';
          const done = step > i || (p === 'done' && i === 2);
          return (
            <View key={name} style={{ flex: 1, borderRadius: 12, padding: 10, gap: 6, borderWidth: 1,
              borderColor: active ? t.primary : t.border, backgroundColor: active ? t.primary + '1A' : 'transparent' }}>
              <View style={{ width: 28, height: 28, borderRadius: 14, alignItems: 'center', justifyContent: 'center',
                backgroundColor: done ? t.success : active ? t.primary : t.section }}>
                <Text style={{ color: done ? t.onSuccess : active ? t.onPrimary : t.foreground, fontWeight: '700' }}>{done ? '✓' : i + 1}</Text>
              </View>
              <Text style={{ color: active || done ? t.foreground : t.muted, fontSize: 15, fontWeight: '600' }}>{name}</Text>
            </View>
          );
        })}
      </View>

      <Panel style={{ padding: 20 }}>
        <Text accessibilityLiveRegion="polite" style={{ color: t.foreground, fontSize: 24, fontWeight: '700', lineHeight: 30 }}>{prompt}</Text>
        <Text style={{ color: t.foreground, fontSize: 64, fontWeight: '700', minHeight: 72, fontVariant: ['tabular-nums'] }}>
          {running ? Math.ceil(s.cal.remaining / 1000) : ' '}
        </Text>
        <View style={{ height: 12, borderRadius: 999, backgroundColor: t.section, overflow: 'hidden' }}>
          <View style={{ height: '100%', backgroundColor: t.primary,
            width: running ? `${(1 - s.cal.remaining / s.cal.phaseMs) * 100}%` : p === 'done' ? '100%' : '0%' }} />
        </View>
      </Panel>

      {p === 'relaxed_captured'
        ? <BigButton title="Start squeeze" height={72} disabled={!(ready || st === 'calibrating')} onPress={() => store.calStep('calibrate_squeeze')} />
        : !running && <BigButton title={p === 'done' || s.calibration ? 'Calibrate again' : p === 'failed' ? 'Start relaxed again' : 'Start relaxed'}
            height={72} disabled={!ready} onPress={() => store.calStep('calibrate_relaxed')} />}
      {(running || p === 'relaxed_captured') && <BigButton title="Cancel" kind="outline" onPress={() => store.calStep('calibrate_cancel')} />}
      {!!hint && <Muted size={16}>{hint}</Muted>}

      <Panel>
        <Muted size={14}>Raw muscle signal (envelope), last 15 s</Muted>
        <LiveLine series={s.series.raw} now={now} windowMs={15000} color={t.activation} floorMax={200} />
      </Panel>
      <Panel>
        <View style={{ flexDirection: 'row' }}>
          {[['Rest level', num(s.calibration?.rest, 0)], ['Max squeeze level', num(s.calibration?.max, 0)],
            ['Calibrated', s.calibration ? timeOfDay(s.calibration.at) : 'Not yet']].map(([k, v]) => (
            <View key={k} style={{ flex: 1 }}>
              <Text style={{ color: t.muted, fontSize: 13 }}>{k}</Text>
              <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>{v}</Text>
            </View>
          ))}
        </View>
      </Panel>
    </Screen>
  );
}
