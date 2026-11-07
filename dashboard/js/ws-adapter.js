/*
 * WebSocketAdapter: talks to Abduljabbar's backend. Same interface as MockAdapter.
 * Untested until the backend exists. Expected backend behaviour:
 *   - pushes JSON messages shaped like contract.js EXAMPLES
 *   - accepts {type:"command", id, name, args}
 *   - replies with {type:"response", command_id: <id>, status, ...}
 * Open the dashboard with ?source=ws&url=ws://localhost:8000/ws to use it.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});
  const TIMEOUT_MS = 5000;

  class WebSocketAdapter extends VG.Emitter {
    constructor(url) {
      super();
      this.kind = 'ws';
      this.url = url;
      this.ws = null;
      this.wantOpen = false;
      this.retryMs = 500;
      this.pending = new Map();
      this.cmdSeq = 0;
      this.isOpen = false;
    }

    connect() {
      this.wantOpen = true;
      if (!this.ws) this._open();
    }

    disconnect() {
      this.wantOpen = false;
      if (this.ws) this.ws.close();
    }

    send(name, args = {}) {
      const id = `cmd-${Date.now().toString(36)}-${++this.cmdSeq}`;
      const fail = (error) => ({ type: 'response', command_id: id, command: name, status: 'error', error });
      return new Promise((resolve) => {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
          resolve(fail('Not connected to the backend.'));
          return;
        }
        const timer = setTimeout(() => {
          this.pending.delete(id);
          resolve(fail('No reply from the backend within 5 s.'));
        }, TIMEOUT_MS);
        this.pending.set(id, (r) => { clearTimeout(timer); resolve(r); });
        this.ws.send(JSON.stringify({ type: 'command', id, name, args }));
      });
    }

    _open() {
      let ws;
      try {
        ws = new WebSocket(this.url);
      } catch (e) {
        this._retry();
        return;
      }
      this.ws = ws;
      ws.onopen = () => {
        this.retryMs = 500;
        this.isOpen = true;
        this.emit('connection', { type: 'connection', connected: true });
      };
      ws.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        this._route(m);
      };
      ws.onclose = () => {
        this.ws = null;
        if (this.isOpen) {
          this.isOpen = false;
          this.emit('connection', { type: 'connection', connected: false, reason: 'Lost connection to the backend' });
        }
        for (const [id, done] of this.pending) {
          done({ type: 'response', command_id: id, status: 'error', error: 'Connection closed.' });
        }
        this.pending.clear();
        if (this.wantOpen) this._retry();
      };
    }

    _retry() {
      setTimeout(() => { if (this.wantOpen && !this.ws) this._open(); }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 5000);
    }

    _route(m) {
      if (!m || typeof m.type !== 'string') return;
      if (m.type === 'response') {
        const done = this.pending.get(m.command_id);
        if (done) { this.pending.delete(m.command_id); done(m); } // resolve on first reply
        this.emit('response', m);
        return;
      }
      const ch = VG.contract.channelFor(m.type);
      if (ch) this.emit(ch, m);
    }
  }

  VG.WebSocketAdapter = WebSocketAdapter;
})();
