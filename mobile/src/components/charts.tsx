/**
 * Small SVG charts (react-native-svg, included in Expo Go): the live lines and the Summary tab's bars, lines,
 * donuts and dots. Thin marks, hairline grid, legends for two or more series. No safe/unsafe lines anywhere.
 */
import { useState, type ReactNode } from 'react';
import { Text, View } from 'react-native';
import Svg, { Circle, G, Line, Path, Polyline, Rect, Text as SvgText } from 'react-native-svg';

import type { Pt } from '../state/store';
import { useTheme } from '../theme';

function useWidth(): [number, (e: { nativeEvent: { layout: { width: number } } }) => void] {
  const [w, setW] = useState(0);
  return [w, (e) => setW(Math.round(e.nativeEvent.layout.width))];
}

const niceMax = (v: number) => {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].map((m) => m * p).find((m) => m >= v) ?? v;
};

const fmtTick = (v: number) => (v >= 10 || v === 0 ? String(Math.round(v)) : v.toFixed(v < 1 ? 2 : 1));

/** A y-axis grid with labels, for the charts below. */
function Grid({ w, h, top, left, max, ticks = 4, suffix = '' }: { w: number; h: number; top: number; left: number; max: number; ticks?: number; suffix?: string }) {
  const t = useTheme();
  return (
    <G>
      {Array.from({ length: ticks + 1 }, (_, i) => {
        const v = (max / ticks) * i;
        const y = top + h - (v / max) * h;
        return (
          <G key={i}>
            <Line x1={left} x2={w} y1={y} y2={y} stroke={t.border} strokeWidth={1} />
            <SvgText x={left - 6} y={y + 4} fill={t.muted} fontSize={11} textAnchor="end">{fmtTick(v) + suffix}</SvgText>
          </G>
        );
      })}
    </G>
  );
}

/** A live line over the last `windowMs` (gaps stay gaps). */
export function LiveLine({ series, now, windowMs = 30000, color, fixedMax, floorMax = 1, height = 140, stale }: {
  series: Pt[]; now: number; windowMs?: number; color: string; fixedMax?: number; floorMax?: number; height?: number; stale?: boolean;
}) {
  const t = useTheme();
  const [w, onLayout] = useWidth();
  const start = now - windowMs;
  const visible = series.filter((p) => p.t >= start);
  const vals = visible.map((p) => p.v).filter((v): v is number => v != null);
  const max = fixedMax ?? niceMax(Math.max(floorMax, ...(vals.length ? vals : [0])) * 1.1);
  const left = 34;
  const plotW = Math.max(1, w - left);
  const segments: string[] = [];
  let cur: string[] = [];
  for (const p of visible) {
    if (p.v == null) { if (cur.length > 1) segments.push(cur.join(' ')); cur = []; continue; }
    const x = left + ((p.t - start) / windowMs) * plotW;
    const y = 4 + (height - 8) - (Math.min(p.v, max) / max) * (height - 8);
    cur.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  if (cur.length > 1) segments.push(cur.join(' '));
  return (
    <View onLayout={onLayout} style={{ height }}>
      {w > 0 && (
        <Svg width={w} height={height}>
          <Grid w={w} h={height - 8} top={4} left={left} max={max} ticks={3} />
          {segments.map((pts, i) => (
            <Polyline key={i} points={pts} fill="none" stroke={stale ? t.muted : color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          ))}
        </Svg>
      )}
    </View>
  );
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  const t = useTheme();
  return (
    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 14, marginTop: 6 }}>
      {items.map((i) => (
        <View key={i.label} style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
          <View style={{ width: 12, height: 12, borderRadius: 3, backgroundColor: i.color }} />
          <Text style={{ color: t.foreground, fontSize: 14 }}>{i.label}</Text>
        </View>
      ))}
    </View>
  );
}

/** Columns per day, stacked by series. */
export function StackedColumns({ days, keys, colors, height = 190 }: {
  days: { label: string; values: Record<string, number> }[]; keys: string[]; colors: Record<string, string>; height?: number;
}) {
  const t = useTheme();
  const [w, onLayout] = useWidth();
  const totals = days.map((d) => keys.reduce((a, k) => a + (d.values[k] || 0), 0));
  const max = niceMax(Math.max(1, ...totals));
  const left = 28, bottom = 22, top = 6;
  const h = height - bottom - top;
  const slot = days.length ? (w - left) / days.length : 0;
  const bw = Math.max(3, Math.min(22, slot * 0.7));
  const every = Math.max(1, Math.ceil(days.length / 5));
  return (
    <View onLayout={onLayout} style={{ height }}>
      {w > 0 && (
        <Svg width={w} height={height}>
          <Grid w={w} h={h} top={top} left={left} max={max} ticks={Math.min(4, max)} />
          {days.map((d, i) => {
            let y = top + h;
            const x = left + slot * i + (slot - bw) / 2;
            return (
              <G key={i}>
                {keys.map((k) => {
                  const v = d.values[k] || 0;
                  if (!v) return null;
                  const bh = (v / max) * h;
                  y -= bh;
                  return <Rect key={k} x={x} y={y + 1} width={bw} height={Math.max(0, bh - 1)} rx={2} fill={colors[k]} />;
                })}
                {i % every === 0 && (
                  <SvgText x={x + bw / 2} y={height - 6} fill={t.muted} fontSize={11} textAnchor="middle">{d.label}</SvgText>
                )}
              </G>
            );
          })}
        </Svg>
      )}
    </View>
  );
}

/** Lines over the recordings (x = order), one axis. */
export function Lines({ series, height = 190, minMax = 1, suffix = '' }: {
  series: { label: string; color: string; values: (number | null)[] }[]; height?: number; minMax?: number; suffix?: string;
}) {
  const [w, onLayout] = useWidth();
  const n = Math.max(0, ...series.map((s) => s.values.length));
  const all = series.flatMap((s) => s.values).filter((v): v is number => v != null);
  const max = niceMax(Math.max(minMax, ...all));
  const left = 38, top = 6, h = height - 12;
  const x = (i: number) => left + (n <= 1 ? (w - left) / 2 : (i / (n - 1)) * (w - left - 6));
  const y = (v: number) => top + h - (v / max) * h;
  return (
    <View onLayout={onLayout} style={{ height }}>
      {w > 0 && (
        <Svg width={w} height={height}>
          <Grid w={w} h={h} top={top} left={left} max={max} suffix={suffix} />
          {series.map((s) => {
            const pts = s.values.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(' ');
            const lastI = s.values.length - 1;
            const last = s.values[lastI];
            return (
              <G key={s.label}>
                <Polyline points={pts} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" />
                {last != null && <Circle cx={x(lastI)} cy={y(last)} r={4} fill={s.color} />}
              </G>
            );
          })}
        </Svg>
      )}
    </View>
  );
}

/** Ranked horizontal bars with the value at the tip. */
export function RankedBars({ rows, color, format }: { rows: { label: string; value: number }[]; color: string; format: (v: number) => string }) {
  const t = useTheme();
  const max = Math.max(...rows.map((r) => r.value), 1e-9);
  return (
    <View style={{ gap: 10 }}>
      {rows.map((r, i) => (
        <View key={i} style={{ gap: 4 }}>
          <Text style={{ color: t.muted, fontSize: 13 }}>{r.label}</Text>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <View style={{ height: 16, width: `${Math.max(2, (r.value / max) * 82)}%`, backgroundColor: color, borderRadius: 4 }} />
            <Text style={{ color: t.foreground, fontSize: 14, fontWeight: '600' }}>{format(r.value)}</Text>
          </View>
        </View>
      ))}
    </View>
  );
}

/** A ring split into parts, with something in the middle. */
export function Donut({ parts, size = 180, center }: { parts: { value: number; color: string }[]; size?: number; center?: ReactNode }) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  const r = size / 2 - 14;
  const c = size / 2;
  let angle = -Math.PI / 2;
  const arcs = total > 0 ? parts.filter((p) => p.value > 0).map((p) => {
    const a0 = angle;
    const a1 = angle + (p.value / total) * Math.PI * 2;
    angle = a1;
    if (p.value === total) return { d: null, color: p.color };
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const d = `M ${c + r * Math.cos(a0)} ${c + r * Math.sin(a0)} A ${r} ${r} 0 ${large} 1 ${c + r * Math.cos(a1)} ${c + r * Math.sin(a1)}`;
    return { d, color: p.color };
  }) : [];
  return (
    <View style={{ width: size, height: size, alignSelf: 'center', alignItems: 'center', justifyContent: 'center' }}>
      <Svg width={size} height={size} style={{ position: 'absolute' }}>
        {arcs.map((a, i) => a.d
          ? <Path key={i} d={a.d} stroke={a.color} strokeWidth={22} fill="none" />
          : <Circle key={i} cx={c} cy={c} r={r} stroke={a.color} strokeWidth={22} fill="none" />)}
      </Svg>
      {center}
    </View>
  );
}

/** One horizontal bar per row, split into parts (e.g. felt / didn't feel / no answer). */
export function StackedRows({ rows, keys, colors }: {
  rows: { label: string; values: Record<string, number> }[]; keys: string[]; colors: Record<string, string>;
}) {
  const t = useTheme();
  const max = Math.max(1, ...rows.map((r) => keys.reduce((a, k) => a + (r.values[k] || 0), 0)));
  return (
    <View style={{ gap: 10 }}>
      {rows.map((r, i) => (
        <View key={i} style={{ gap: 4 }}>
          <Text style={{ color: t.muted, fontSize: 13 }}>{r.label}</Text>
          <View style={{ flexDirection: 'row', height: 18, gap: 2 }}>
            {keys.map((k) => (r.values[k] ? (
              <View key={k} style={{ width: `${(r.values[k] / max) * 100}%`, backgroundColor: colors[k], borderRadius: 3 }} />
            ) : null))}
          </View>
        </View>
      ))}
    </View>
  );
}

/** Each dot is one recording; colour by source. */
export function Dots({ points, height = 210, xLabel, yLabel }: {
  points: { x: number; y: number; color: string }[]; height?: number; xLabel: string; yLabel: string;
}) {
  const t = useTheme();
  const [w, onLayout] = useWidth();
  const xMax = niceMax(Math.max(1, ...points.map((p) => p.x)));
  const yMax = niceMax(Math.max(100, ...points.map((p) => p.y)));
  const left = 34, top = 6, bottom = 20, h = height - top - bottom;
  return (
    <View>
      <Text style={{ color: t.muted, fontSize: 12, marginBottom: 2 }}>{yLabel}</Text>
      <View onLayout={onLayout} style={{ height }}>
        {w > 0 && (
          <Svg width={w} height={height}>
            <Grid w={w} h={h} top={top} left={left} max={yMax} />
            {[0, 0.5, 1].map((f) => (
              <SvgText key={f} x={left + f * (w - left - 8)} y={height - 4} fill={t.muted} fontSize={11} textAnchor="middle">{fmtTick(f * xMax)}</SvgText>
            ))}
            {points.map((p, i) => (
              <Circle key={i} cx={left + (p.x / xMax) * (w - left - 8)} cy={top + h - (p.y / yMax) * h} r={6} fill={p.color} stroke={t.card} strokeWidth={2} />
            ))}
          </Svg>
        )}
      </View>
      <Text style={{ color: t.muted, fontSize: 12, textAlign: 'center' }}>{xLabel}</Text>
    </View>
  );
}
