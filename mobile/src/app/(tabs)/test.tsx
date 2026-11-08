/** Touch test: the softest buzz the person can feel at the fingertip. Very large answer buttons. */
import { Ionicons } from '@expo/vector-icons';
import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { Screen } from '../../components/frame';
import { useTick } from '../../components/tick';
import { BigButton, Eyebrow, Muted, Note, Panel } from '../../components/ui';
import { store, useStore } from '../../state/store';
import { useTheme } from '../../theme';
import { num } from '../../vg/format.ts';

export default function TouchTest() {
  const t = useTheme();
  const s = useStore();
  const now = useTick(100);
  const [mode, setMode] = useState<'preview' | 'full'>('preview');
  const st = s.st;
  const station = s.devices.station.state;

  return (
    <Screen device="station">
      <View style={{ gap: 4 }}>
        <Eyebrow>Touch test</Eyebrow>
        <Text style={{ color: t.foreground, fontSize: 24, fontWeight: '700' }}>Fingertip vibration test</Text>
        {s.mode === 'demo' && <Muted>Practice station: no motor is driven, nothing here is a measurement.</Muted>}
      </View>

      {st.view === 'idle' && (
        <Panel style={{ padding: 18, gap: 14 }}>
          <Text style={{ color: t.foreground, fontSize: 18, lineHeight: 25 }}>Checks the softest buzz the person can feel. Rest a fingertip on the motor, then answer each time: felt it, or didn’t.</Text>
          <Muted>Test length</Muted>
          {(['preview', 'full'] as const).map((m) => (
            <Pressable key={m} accessibilityRole="radio" accessibilityState={{ selected: mode === m }} onPress={() => setMode(m)}
              style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 68, padding: 14, borderRadius: 12, borderWidth: 1,
                borderColor: mode === m ? t.primary : t.border, backgroundColor: mode === m ? t.primary + '1A' : t.background }}>
              <Ionicons name={mode === m ? 'radio-button-on' : 'radio-button-off'} size={26} color={mode === m ? t.primary : t.muted} />
              <View>
                <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>{m === 'preview' ? 'Quick test' : 'Full test'}</Text>
                <Text style={{ color: t.muted, fontSize: 15 }}>{m === 'preview' ? 'About 30 seconds (6 trials)' : 'A few minutes (until 8 reversals)'}</Text>
              </View>
            </Pressable>
          ))}
          <BigButton title="Start test" height={72} disabled={station !== 'ready'} onPress={() => store.startTest(mode)} />
          {station === 'disconnected' && <Muted>Connect to the laptop first.</Muted>}
        </Panel>
      )}

      {st.view === 'running' && (() => {
        const c = st.current;
        const n = c ? c.index : st.trials.length;
        const waiting = !!c && !c.answered;
        const left = c && !c.answered ? Math.max(0, 1 - (now - c.receivedAt) / c.response_window_ms) : 0;
        return (
          <Panel style={{ padding: 18, gap: 16 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
              <Text style={{ color: t.muted, fontSize: 16 }}>{n ? (st.planned ? `Trial ${n} of ${st.planned}` : `Trial ${n}`) : 'Starting'}</Text>
              <View style={{ width: 150 }}><BigButton title="Stop test" kind="danger" height={48} onPress={() => store.abortTest()} /></View>
            </View>
            <Text accessibilityLiveRegion="polite" style={{ color: t.foreground, fontSize: 28, fontWeight: '800' }}>
              {!c ? 'Keep your fingertip on the motor. Next trial coming.' : c.answered ? 'Answer recorded.' : 'Did you feel a vibration?'}
            </Text>
            <View style={{ height: 10, borderRadius: 999, backgroundColor: t.section, overflow: 'hidden' }}>
              <View style={{ height: '100%', width: `${left * 100}%`, backgroundColor: t.primary }} />
            </View>
            <View style={{ flexDirection: 'row', gap: 12 }}>
              <Answer title="Felt it" icon="checkmark" color={t.success} disabled={!waiting} onPress={() => store.respond('felt')} />
              <Answer title="Didn’t feel it" icon="close" color={t.foreground} disabled={!waiting} onPress={() => store.respond('not_felt')} />
            </View>
            <Muted>No answer in time is saved as “no answer”, not as “didn’t feel it”.</Muted>
          </Panel>
        );
      })()}

      {st.view === 'complete' && st.result && (
        <Panel style={{ padding: 18 }}>
          <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <Text style={{ color: t.foreground, fontSize: 54, fontWeight: '700' }}>{num(st.result.threshold_setting, 0)}</Text>
            <Text style={{ color: t.muted, fontSize: 16 }}>threshold, motor setting out of 255</Text>
          </View>
          <Muted>{st.result.threshold_setting == null ? 'Not enough reversals to estimate a threshold. Run the full test for one.'
            : `Average of the last ${st.result.reversals?.length ?? 0} reversal settings.`}
            {st.result.missing_responses ? ` ${st.result.missing_responses} with no answer.` : ''}
            {st.result.provenance === 'simulated' ? ' Simulated station: no motor was driven.' : ''}</Muted>
          <BigButton title="Run another test" kind="outline" onPress={() => store.testAgain()} />
        </Panel>
      )}

      {!!st.msg && <Note text={st.msg} kind="danger" />}

      {st.trials.length > 0 && (
        <Panel>
          <Text style={{ color: t.foreground, fontSize: 16, fontWeight: '700' }}>Trials in this test</Text>
          {st.trials.map((tr) => (
            <View key={tr.trial_id} style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
              <Text style={{ color: t.muted, fontSize: 15 }}>Trial {tr.index}</Text>
              <Text style={{ color: t.foreground, fontSize: 15 }}>{st.view === 'complete' || !tr.is_catch ? `setting ${tr.stimulus_setting}` : '—'}</Text>
              <Text style={{ color: tr.response === 'missing' ? t.destructive : t.foreground, fontSize: 15, fontWeight: '600' }}>
                {tr.response === 'felt' ? 'Felt it' : tr.response === 'not_felt' ? 'Didn’t feel it' : 'No answer'}
              </Text>
            </View>
          ))}
        </Panel>
      )}
      <Muted size={14}>Motor settings are drive levels sent to the motor, not measured vibration amplitudes.</Muted>
    </Screen>
  );
}

function Answer({ title, icon, color, disabled, onPress }: { title: string; icon: 'checkmark' | 'close'; color: string; disabled: boolean; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={title} disabled={disabled} onPress={onPress}
      style={({ pressed }) => ({ flex: 1, minHeight: 150, borderRadius: 16, borderWidth: 2, borderColor: color + (disabled ? '44' : 'AA'),
        backgroundColor: color + (pressed ? '33' : '1A'), alignItems: 'center', justifyContent: 'center', gap: 10, opacity: disabled ? 0.5 : 1 })}>
      <Ionicons name={icon} size={46} color={color} />
      <Text style={{ color, fontSize: 21, fontWeight: '700', textAlign: 'center' }}>{title}</Text>
    </Pressable>
  );
}
