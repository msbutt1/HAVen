/** Building blocks: panels, big buttons, labels, the data-source banner. Large targets and type throughout. */
import type { ReactNode } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';
import Svg, { Defs, Pattern, Polygon, Rect } from 'react-native-svg';

import { type Theme, useTheme } from '../theme';

export function Panel({ children, style, dashed }: { children: ReactNode; style?: StyleProp<ViewStyle>; dashed?: boolean }) {
  const t = useTheme();
  return (
    <View style={[{ backgroundColor: t.card, borderColor: dashed ? t.muted : t.border, borderStyle: dashed ? 'dashed' : 'solid',
      borderWidth: 1, borderRadius: 16, padding: 16, gap: 10 }, style]}>
      {children}
    </View>
  );
}

type Kind = 'primary' | 'outline' | 'danger';

export function BigButton({ title, onPress, kind = 'primary', disabled, busy, height = 60, icon }: {
  title: string; onPress: () => void; kind?: Kind; disabled?: boolean; busy?: boolean; height?: number; icon?: ReactNode;
}) {
  const t = useTheme();
  const bg = kind === 'primary' ? t.primary : kind === 'danger' ? t.destructive : t.section;
  const fg = kind === 'primary' ? t.onPrimary : kind === 'danger' ? '#FFFFFF' : t.foreground;
  return (
    <Pressable accessibilityRole="button" accessibilityState={{ disabled: !!disabled, busy: !!busy }}
      onPress={onPress} disabled={disabled || busy}
      style={({ pressed }) => [{ minHeight: height, borderRadius: 12, backgroundColor: bg, paddingHorizontal: 18,
        alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 10,
        borderWidth: kind === 'outline' ? 1 : 0, borderColor: t.border, opacity: disabled ? 0.45 : pressed ? 0.85 : 1 }]}>
      {busy ? <ActivityIndicator color={fg} /> : icon}
      <Text style={{ color: fg, fontSize: 19, fontWeight: '600', textAlign: 'center' }}>{title}</Text>
    </Pressable>
  );
}

export function Title({ children }: { children: ReactNode }) {
  const t = useTheme();
  return <Text accessibilityRole="header" style={{ color: t.foreground, fontSize: 18, fontWeight: '700' }}>{children}</Text>;
}

export function Muted({ children, size = 15 }: { children: ReactNode; size?: number }) {
  const t = useTheme();
  return <Text style={{ color: t.muted, fontSize: size, lineHeight: size * 1.35 }}>{children}</Text>;
}

export function Eyebrow({ children }: { children: ReactNode }) {
  const t = useTheme();
  return <Text style={{ color: t.muted, fontSize: 12, letterSpacing: 2, fontWeight: '600' }}>{String(children).toUpperCase()}</Text>;
}

/**
 * The violet stripes that mark simulated data everywhere in HAVen: diagonal bands drawn in the tile itself
 * (react-native-svg does not rotate patterns the way browsers do).
 */
export function Stripes({ t, radius = 10 }: { t: Theme; radius?: number }) {
  const s = 24;
  return (
    <View style={[StyleSheet.absoluteFill, { borderRadius: radius, overflow: 'hidden' }]} pointerEvents="none">
      <Svg width="100%" height="100%">
        <Defs>
          <Pattern id="vgStripes" patternUnits="userSpaceOnUse" width={s} height={s}>
            <Rect width={s} height={s} fill={t.sim} />
            <Polygon points={`0,0 ${s / 2},0 0,${s / 2}`} fill={t.sim2} />
            <Polygon points={`${s},0 ${s},${s / 2} ${s / 2},${s} 0,${s}`} fill={t.sim2} />
          </Pattern>
        </Defs>
        <Rect width="100%" height="100%" fill="url(#vgStripes)" />
      </Svg>
    </View>
  );
}

export function SourceTag({ source }: { source: 'live' | 'replay' | 'simulated' }) {
  const t = useTheme();
  const text = source === 'live' ? 'Live' : source === 'replay' ? 'Replay' : 'Simulated';
  return (
    <View style={{ borderRadius: 999, paddingHorizontal: 10, paddingVertical: 3, overflow: 'hidden',
      backgroundColor: source === 'live' ? t.success : source === 'replay' ? t.primary : t.sim }}>
      {source === 'simulated' && <Stripes t={t} radius={999} />}
      <Text style={{ color: source === 'live' ? t.onSuccess : source === 'replay' ? t.onPrimary : '#FFFFFF', fontSize: 13, fontWeight: '600' }}>{text}</Text>
    </View>
  );
}

export function ExperimentalTag() {
  const t = useTheme();
  return (
    <View style={{ borderRadius: 999, borderWidth: 1, borderStyle: 'dashed', borderColor: t.muted, paddingHorizontal: 8, paddingVertical: 1 }}>
      <Text style={{ color: t.muted, fontSize: 12 }}>Experimental</Text>
    </View>
  );
}

/** A warning line: icon-free, coloured box, plain words. */
export function Note({ text, kind = 'warning' }: { text: string; kind?: 'warning' | 'danger' }) {
  const t = useTheme();
  const c = kind === 'danger' ? t.destructive : t.warning;
  return (
    <View style={{ borderRadius: 10, borderWidth: 1, borderColor: c + '88', backgroundColor: c + '22', padding: 12 }}>
      <Text style={{ color: t.foreground, fontSize: 15, fontWeight: '500' }}>{text}</Text>
    </View>
  );
}
