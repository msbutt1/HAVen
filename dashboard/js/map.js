/*
 * Workers on a map: one dot per worker at the position their phone reported, coloured by whether their
 * sleeve is sending fresh data, with the sleeve's readings in the popup and in the list beside the map.
 *
 * What the dots do NOT say: safe or unsafe. The numbers come from the sleeve pipeline (activation relative to
 * that wearer's calibration, measured vibration when it could be converted, the EXPERIMENTAL exposure score);
 * none of them has a validated limit behind it. Colour only means "the sleeve is sending data / is not".
 *
 * Everything that came from the network (labels, ids) is put on the page with textContent, never as HTML.
 * Map tiles are OpenStreetMap's: the viewer's browser contacts tile.openstreetmap.org (see the page note).
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const STATUS = {
    ok: { text: 'Sleeve sending data', color: '#1e7f46' },
    stale: { text: 'Sleeve connected, no fresh data', color: '#f4b000' },
    offline: { text: 'Sleeve offline', color: '#6b7780' },
    no_sleeve: { text: 'No sleeve assigned', color: '#8a6fb8' },
  };
  const POLL_MS = 2000;
  const fmt = (v, dp) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(dp));

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  class WorkerMap {
    constructor({ mapEl, listEl, noteEl, adapter }) {
      this.mapEl = mapEl;
      this.listEl = listEl;
      this.noteEl = noteEl;
      this.adapter = adapter;
      this.map = null;
      this.layers = new Map(); // worker id -> { dot, halo, trail }
      this.timer = null;
      this.fitted = false;
      this.userMoved = false;
      this.busy = false;
      this.workers = [];
    }

    show() {
      if (typeof L === 'undefined') {
        this.noteEl.textContent = 'The map library did not load, so the map cannot be drawn.';
        return;
      }
      if (!this.map) this._init();
      setTimeout(() => this.map.invalidateSize(), 0); // the container was hidden when it was created
      this.refresh();
      if (!this.timer) this.timer = setInterval(() => { if (!document.hidden) this.refresh(); }, POLL_MS);
    }

    hide() {
      clearInterval(this.timer);
      this.timer = null;
    }

    _init() {
      this.map = L.map(this.mapEl, { worldCopyJump: true }).setView([41.3111, 69.2797], 3);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
      }).addTo(this.map);
      // once the person pans or zooms, stop re-fitting the view to the workers
      this.map.on('movestart zoomstart', () => { if (!this._programmatic) this.userMoved = true; });
    }

    async refresh() {
      if (this.busy) return;
      this.busy = true;
      try {
        const r = await this.adapter.workers();
        if (r.error) {
          this.noteEl.textContent = r.error;
          return;
        }
        this.workers = r.workers;
        this._draw(r.workers);
      } finally {
        this.busy = false;
      }
    }

    _draw(workers) {
      const withPos = workers.filter((w) => w.position);
      const sim = withPos.filter((w) => w.position.source === 'simulated').length;
      const parts = [`${withPos.length} of ${workers.length} worker${workers.length === 1 ? '' : 's'} sharing a position.`];
      if (sim) parts.push(`${sim === withPos.length ? 'All' : sim} position${sim === 1 ? ' is' : 's are'} SIMULATED (not real people).`);
      this.noteEl.textContent = parts.join(' ');

      const seen = new Set();
      const bounds = [];
      for (const w of withPos) {
        seen.add(w.worker_id);
        const p = w.position;
        const ll = [p.lat, p.lon];
        bounds.push(ll);
        const st = STATUS[w.status] || STATUS.offline;
        let lay = this.layers.get(w.worker_id);
        if (!lay) {
          lay = { halo: L.circle(ll, { radius: 1, weight: 0, fillOpacity: 0.12, interactive: false }).addTo(this.map),
                  trail: L.polyline([], { weight: 2, opacity: 0.5, interactive: false }).addTo(this.map),
                  dot: L.circleMarker(ll, { radius: 9, weight: 2, color: '#ffffff' }).addTo(this.map) };
          lay.dot.bindPopup(() => this._popup(lay.worker), { maxWidth: 300 });
          this.layers.set(w.worker_id, lay);
        }
        lay.worker = w;
        lay.dot.setLatLng(ll).setStyle({
          fillColor: st.color, fillOpacity: p.stale ? 0.4 : 0.95,
          dashArray: p.source === 'simulated' ? '3 3' : null,
        });
        lay.halo.setLatLng(ll).setRadius(Math.max(p.accuracy_m || 0, 1)).setStyle({ fillColor: st.color });
        lay.trail.setLatLngs(w.trail || []).setStyle({ color: st.color });
        if (lay.dot.isPopupOpen()) lay.dot.getPopup().setContent(this._popup(w));
      }
      for (const [id, lay] of this.layers) {
        if (!seen.has(id)) {
          [lay.dot, lay.halo, lay.trail].forEach((x) => this.map.removeLayer(x));
          this.layers.delete(id);
        }
      }
      if (bounds.length && !this.fitted && !this.userMoved) {
        this._programmatic = true;
        this.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
        setTimeout(() => { this._programmatic = false; }, 900);
        this.fitted = true;
      }
      this._list(workers);
    }

    _popup(w) {
      const root = el('div', null, 'wm-popup');
      root.appendChild(el('strong', w.label || w.worker_id));
      const s = w.sleeve || {};
      const st = STATUS[w.status] || STATUS.offline;
      root.appendChild(el('div', st.text, 'wm-status'));
      const rows = [];
      if (w.position) {
        rows.push(['Position', `${w.position.source === 'simulated' ? 'simulated' : 'from the worker’s phone'}, ${Math.round(w.position.age_s)} s ago${w.position.stale ? ' (last known)' : ''}${w.position.accuracy_m != null ? `, ±${Math.round(w.position.accuracy_m)} m` : ''}`]);
      }
      if (w.status !== 'no_sleeve') {
        rows.push(['Sleeve data', s.source ? `${s.source}${s.state ? `, ${s.state}` : ''}` : '—']);
        rows.push(['Muscle activation', s.calibrated && s.activation_pct != null ? `${fmt(s.activation_pct, 0)} % of this person’s calibrated squeeze` : s.calibrated ? '—' : 'not calibrated']);
        rows.push(['Vibration', s.vibration_ms2 != null ? `${fmt(s.vibration_ms2, 2)} m/s² (measured, unweighted)` : '— (not converted to m/s²)']);
        rows.push(['Exposure score', s.score_rate_au_h != null ? `${fmt(s.score_rate_au_h, 3)} au/h, experimental` : '—']);
      }
      const dl = el('dl');
      for (const [k, v] of rows) { dl.appendChild(el('dt', k)); dl.appendChild(el('dd', v)); }
      root.appendChild(dl);
      root.appendChild(el('div', 'Experimental values: not a dose and not a safety limit.', 'wm-note'));
      return root;
    }

    _list(workers) {
      this.listEl.textContent = '';
      if (!workers.length) {
        this.listEl.appendChild(el('li', 'No workers yet. A worker appears when their phone shares a position (open phone.html on the phone) or when a demo fleet is running.', 'empty'));
        return;
      }
      for (const w of workers) {
        const st = STATUS[w.status] || STATUS.offline;
        const s = w.sleeve || {};
        const li = el('li');
        const b = el('button', null, 'wm-item');
        b.type = 'button';
        const dot = el('span', null, 'wm-swatch');
        dot.style.background = st.color;
        dot.setAttribute('aria-hidden', 'true');
        b.appendChild(dot);
        const text = el('span');
        text.appendChild(el('strong', w.label || w.worker_id));
        text.appendChild(document.createTextNode(` · ${st.text}`));
        if (w.position && w.position.source === 'simulated') text.appendChild(el('span', 'Simulated', 'tag tag-sim'));
        const detail = [];
        if (s.activation_pct != null) detail.push(`activation ${fmt(s.activation_pct, 0)} %`);
        if (s.vibration_ms2 != null) detail.push(`vibration ${fmt(s.vibration_ms2, 2)} m/s²`);
        if (s.score_rate_au_h != null) detail.push(`score ${fmt(s.score_rate_au_h, 3)} au/h`);
        detail.push(w.position ? `position ${Math.round(w.position.age_s)} s ago${w.position.stale ? ' (last known)' : ''}` : 'not sharing a position');
        text.appendChild(el('span', detail.join(' · '), 'wm-detail'));
        b.appendChild(text);
        b.addEventListener('click', () => {
          const lay = this.layers.get(w.worker_id);
          if (!lay) return;
          this.userMoved = true;
          this.map.setView(lay.dot.getLatLng(), Math.max(this.map.getZoom(), 16));
          lay.dot.openPopup();
        });
        li.appendChild(b);
        this.listEl.appendChild(li);
      }
    }
  }

  VG.WorkerMap = WorkerMap;
})();
