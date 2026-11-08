/** History: saved recordings and touch tests, newest first, as dated cards, 20 at a time. */
import { useState } from 'react';
import { Text, View } from 'react-native';

import { Screen } from '../../components/frame';
import { BigButton, Eyebrow, Muted, Panel, SourceTag } from '../../components/ui';
import { useStore } from '../../state/store';
import { useTheme } from '../../theme';
import { duration, num, score, when } from '../../vg/format.ts';
import type { Saved } from '../../vg/summary.ts';

export default function History() {
  const t = useTheme();
  const s = useStore();
  const [shown, setShown] = useState(20);
  return (
    <Screen>
      <View style={{ gap: 4 }}>
        <Eyebrow>History</Eyebrow>
        <Text style={{ color: t.foreground, fontSize: 24, fontWeight: '700' }}>Saved recordings and tests</Text>
      </View>
      {s.saved.length === 0
        ? <Panel><Muted size={17}>{s.connected ? 'Nothing saved yet. Record on the Live screen or run a touch test.' : 'Connect to the laptop to see saved recordings.'}</Muted></Panel>
        : s.saved.slice(0, shown).map((item) => <Row key={item.kind + (item.kind === 'session' ? item.rec.session_id : item.rec.test_id)} item={item} />)}
      {s.saved.length > shown && <BigButton title={`Show older (${s.saved.length - shown} more)`} kind="outline" onPress={() => setShown(shown + 20)} />}
    </Screen>
  );
}

function Row({ item }: { item: Saved }) {
  const t = useTheme();
  let facts: [string, string][];
  let warning: string | null = null;
  if (item.kind === 'session') {
    const r = item.rec;
    facts = [['Length', duration(r.duration_s)]];
    if (r.mean_activation_pct != null) facts.push(['Mean grip effort', `${num(r.mean_activation_pct, 0)} %`]);
    if (r.peak_vib_ahv_est != null) facts.push(['Peak vibration', `${num(r.peak_vib_ahv_est, 2)} m/s²`]);
    if (r.tool_time_s != null) facts.push(['Tool time (estimated)', duration(r.tool_time_s)]);
    facts.push(['Score (experimental)', r.score_total != null ? `${score(r.score_total)} ${r.score_units || 'au'}`
      : r.calibrated ? 'none for this recording' : 'none (not calibrated)']);
    if (r.ended_reason !== 'stopped') warning = `Ended early: ${r.ended_reason}`;
  } else {
    const r = item.rec;
    facts = [['Threshold', r.threshold_setting == null ? 'not reached' : `setting ${num(r.threshold_setting, 0)}`], ['Trials', String(r.trials.length)]];
    if (r.missing_responses) facts.push(['No answer', String(r.missing_responses)]);
    facts.push(['Type', r.mode === 'preview' ? 'Quick test' : 'Full test']);
  }
  return (
    <Panel style={{ gap: 12 }}>
      <View style={{ flexDirection: 'row', alignItems: 'flex-start', gap: 8 }}>
        <View style={{ flex: 1 }}>
          <Text style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>{item.kind === 'session' ? 'Wearable recording' : 'Touch test'}</Text>
          <Text style={{ color: t.muted, fontSize: 15 }}>{when(item.at)}</Text>
        </View>
        <SourceTag source={item.rec.provenance} />
      </View>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', rowGap: 10 }}>
        {facts.map(([k, v]) => (
          <View key={k} style={{ width: '50%' }}>
            <Text style={{ color: t.muted, fontSize: 13 }}>{k}</Text>
            <Text style={{ color: t.foreground, fontSize: 17, fontWeight: '700' }}>{v}</Text>
          </View>
        ))}
      </View>
      {warning && <Text style={{ color: t.destructive, fontSize: 15 }}>{warning}</Text>}
    </Panel>
  );
}
