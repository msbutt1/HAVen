/**
 * The web dashboard's adapters, loaded as they are (dashboard/js), so the phone follows exactly the same rules:
 * BackendAdapter (live WebSocket, commands, history, the touch-test runner) and MockAdapter (the practice feed with
 * its labelled sample history). They are plain scripts that attach to `window.VG`; React Native has WebSocket, fetch
 * and performance, and `window` is pointed at the global object here before they load.
 */
const g = globalThis as any;
if (!g.window) g.window = g;

require('../../../dashboard/js/contract.js');
require('../../../dashboard/js/mock-adapter.js');
require('../../../dashboard/js/backend-station.js');
require('../../../dashboard/js/backend-adapter.js');

export interface Adapter {
  connect(): void;
  disconnect(reason?: string): void;
  send(name: string, args?: Record<string, unknown>): Promise<{ status: string; error?: string; [k: string]: unknown }>;
  on(channel: string, fn: (m: any) => void): void;
  // practice feed only
  setSqueeze?(on: boolean): void;
  setVibration?(on: boolean): void;
  simulateDrop?(): void;
  injectError?(): void;
}

export const VG = g.window.VG as {
  BackendAdapter: new (opts: { base: string; authorization?: string | null; client?: string; runtime?: string }) => Adapter;
  MockAdapter: new () => Adapter;
};
