import { useEffect, useState } from 'react';

/** Redraw every `ms`: staleness, countdowns and the recording timer move with time, not only with data. */
export function useTick(ms = 250): number {
  const [, set] = useState(0);
  useEffect(() => {
    const id = setInterval(() => set((n) => n + 1), ms);
    return () => clearInterval(id);
  }, [ms]);
  return performance.now();
}
