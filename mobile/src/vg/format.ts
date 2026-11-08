/** Number and time formatting shared by the screens (the web dashboard's rules). */

export const num = (v: number | null | undefined, dp: number): string =>
  v == null || !Number.isFinite(v) ? '—' : v.toFixed(dp);

/** Scores below 1 keep three decimals (a short recording scores little). */
export const score = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '—' : Math.abs(v) < 1 ? v.toFixed(3) : v.toFixed(1);

/** 45 s, 12 min, 1 h 05 min. */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, '0')} min`;
}

/** 0:42 for a running recording. */
export const clock = (ms: number): string => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

/** "Today, 3:17 PM", "Yesterday, 11:39 AM", "Thu 5 Nov, 10:37 AM". */
export function when(iso: string | number | Date | null | undefined, now: Date = new Date()): string {
  if (iso == null) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const ago = Math.round((startOfDay(now).getTime() - startOfDay(d).getTime()) / 86400000);
  if (ago === 0) return `Today, ${time}`;
  if (ago === 1) return `Yesterday, ${time}`;
  const opts: Intl.DateTimeFormatOptions = { weekday: 'short', day: 'numeric', month: 'short' };
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return `${d.toLocaleDateString([], opts)}, ${time}`;
}

export const timeOfDay = (iso: string | number | null | undefined): string =>
  iso == null ? '—' : new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
