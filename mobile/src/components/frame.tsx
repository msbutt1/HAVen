/** The frame every main screen shares: where the data comes from, the device that matters, then the content. */
import type { ReactNode } from 'react';
import { ScrollView, Text, View } from 'react-native';

import { freshness, useStore } from '../state/store';
import { useTheme } from '../theme';
import { Stripes } from './ui';

export function Screen({ children, device = 'sleeve' }: { children: ReactNode; device?: 'sleeve' | 'station' }) {
  const t = useTheme();
  return (
    <ScrollView style={{ backgroundColor: t.background }} contentContainerStyle={{ padding: 16, gap: 14, paddingBottom: 40 }}>
      <SourceBanner />
      <DeviceChip role={device} />
      {children}
    </ScrollView>
  );
}

export function SourceBanner() {
  const t = useTheme();
  const s = useStore();
  const src = s.connected && s.latest ? s.latest.provenance : null;
  const text = src === 'live' ? `Live data from ${s.latest?.device_id ?? 'the sleeve'}`
    : src === 'replay' ? 'Recorded replay. This is not live.'
    : src === 'simulated' ? (s.mode === 'demo' ? 'Practice mode: made-up data, not real measurements.' : 'Simulated data. These are not real measurements.')
    : s.connected ? 'Connected. Waiting for the first reading.' : 'Not connected to the laptop. It keeps trying.';
  const bg = src === 'live' ? t.success : src === 'replay' ? t.primary : src === 'simulated' ? t.sim : t.section;
  const fg = src === 'live' ? t.onSuccess : src === 'replay' ? t.onPrimary : src === 'simulated' ? '#FFFFFF' : t.muted;
  return (
    <View accessibilityRole="header" style={{ borderRadius: 10, paddingVertical: 10, paddingHorizontal: 12, backgroundColor: bg, overflow: 'hidden',
      borderWidth: src ? 0 : 1, borderColor: t.border }}>
      {src === 'simulated' && <Stripes t={t} />}
      <Text style={{ color: fg, fontSize: 15, fontWeight: '600', textAlign: 'center' }}>{text}</Text>
    </View>
  );
}

const STATE_LABEL: Record<string, string> = {
  disconnected: 'Not connected', ready: 'Ready', calibrating: 'Calibrating', recording: 'Recording', testing: 'Testing',
  completed: 'Completed', error: 'Error',
};

export function DeviceChip({ role }: { role: 'sleeve' | 'station' }) {
  const t = useTheme();
  const s = useStore();
  const d = s.devices[role];
  const color = d.state === 'ready' || d.state === 'completed' ? t.success : d.state === 'recording' || d.state === 'error' ? t.destructive
    : d.state === 'disconnected' ? t.muted : t.warning;
  const f = freshness(s);
  return (
    <View style={{ gap: 6 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <View style={{ width: 10, height: 10, borderRadius: 5, backgroundColor: color }} />
        <Text style={{ color: t.muted, fontSize: 16 }}>{role === 'sleeve' ? 'Sleeve' : 'Touch-test station'}</Text>
        <Text style={{ color: t.foreground, fontSize: 16, fontWeight: '600' }}>{STATE_LABEL[d.state] ?? d.state}</Text>
      </View>
      {d.state === 'error' && !!d.detail && <Text style={{ color: t.destructive, fontSize: 15 }}>{d.detail}</Text>}
      {role === 'sleeve' && f === 'stale' && (
        <Text style={{ color: t.destructive, fontSize: 15, fontWeight: '600' }}>No new reading for a moment. Values are hidden until data resumes.</Text>
      )}
    </View>
  );
}
