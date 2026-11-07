/*
 * Small canvas charts with no external libraries, so the dashboard works offline.
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function prep(canvas) {
    const r = canvas.getBoundingClientRect();
    const d = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * d));
    const h = Math.max(1, Math.round(r.height * d));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h, d };
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  function fmt(v) {
    return Math.abs(v) >= 10 || v === 0 ? String(Math.round(v)) : v.toFixed(1);
  }

  // Draws y gridlines + labels; returns plot rect.
  function frame(ctx, w, h, d, min, max, lines = 4) {
    const left = 34 * d, right = 6 * d, top = 8 * d, bottom = 8 * d;
    const pw = w - left - right, ph = h - top - bottom;
    ctx.font = `${11 * d}px ${css('--font')}`;
    ctx.fillStyle = css('--ink-2');
    ctx.strokeStyle = css('--line');
    ctx.lineWidth = d;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= lines; i++) {
      const v = min + ((max - min) * i) / lines;
      const y = top + ph - (ph * i) / lines;
      ctx.beginPath();
      ctx.moveTo(left, Math.round(y) + 0.5);
      ctx.lineTo(left + pw, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillText(fmt(v), left - 6 * d, y);
    }
    return { left, top, pw, ph };
  }

  class LineChart {
    constructor(canvas, opts) {
      this.c = canvas;
      this.o = Object.assign({ windowMs: 30000, min: 0, max: null, floorMax: 1, colorVar: '--ink', dashed: false }, opts);
      this.pts = [];
      this.stale = false;
    }
    push(t, v) {
      this.pts.push([t, v]);
      const cut = t - this.o.windowMs - 1000;
      while (this.pts.length && this.pts[0][0] < cut) this.pts.shift();
    }
    clear() { this.pts = []; }
    draw(now) {
      const { ctx, w, h, d } = prep(this.c);
      let max = this.o.max;
      if (max == null) {
        let m = this.o.floorMax;
        for (const p of this.pts) if (p[1] != null && p[1] > m) m = p[1];
        max = niceMax(m * 1.1);
      }
      const min = this.o.min;
      const r = frame(ctx, w, h, d, min, max);
      ctx.strokeStyle = this.stale ? css('--ink-2') : css(this.o.colorVar);
      ctx.globalAlpha = this.stale ? 0.45 : 1;
      ctx.lineWidth = 2 * d;
      ctx.lineJoin = 'round';
      ctx.setLineDash(this.o.dashed ? [6 * d, 4 * d] : []);
      ctx.beginPath();
      let pen = false;
      for (const [t, v] of this.pts) {
        if (v == null) { pen = false; continue; }
        const x = r.left + r.pw * (1 - (now - t) / this.o.windowMs);
        if (x < r.left) continue;
        const y = r.top + r.ph * (1 - (Math.min(max, Math.max(min, v)) - min) / (max - min));
        if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        pen = true;
      }
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    }
  }

  // Weekly chart for the simulated history view: bars or a line, optional reference lines.
  function drawWeekly(canvas, o) {
    const { ctx, w, h, d } = prep(canvas);
    const n = o.values.length;
    const max = o.max || niceMax(Math.max(...o.values, ...(o.refs || []).map((r) => r.value)) * 1.1);
    const r0 = frame(ctx, w, h - 16 * d, d, 0, max);
    const step = r0.pw / n;
    const xAt = (i) => r0.left + step * (i + 0.5);
    const yAt = (v) => r0.top + r0.ph * (1 - v / max);

    // week labels
    ctx.fillStyle = css('--ink-2');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    o.labels.forEach((l, i) => ctx.fillText(l, xAt(i), h - 13 * d));

    const color = css(o.colorVar);
    if (o.kind === 'bars') {
      ctx.fillStyle = color;
      o.values.forEach((v, i) => {
        const bw = step * 0.56;
        ctx.fillRect(xAt(i) - bw / 2, yAt(v), bw, r0.top + r0.ph - yAt(v));
      });
    } else {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5 * d;
      ctx.beginPath();
      o.values.forEach((v, i) => (i ? ctx.lineTo(xAt(i), yAt(v)) : ctx.moveTo(xAt(i), yAt(v))));
      ctx.stroke();
      o.values.forEach((v, i) => {
        const flagged = o.flagFrom != null && i >= o.flagFrom;
        ctx.beginPath();
        ctx.arc(xAt(i), yAt(v), (flagged ? 5 : 3.5) * d, 0, Math.PI * 2);
        ctx.fillStyle = flagged ? css('--danger') : color;
        ctx.fill();
      });
    }

    for (const ref of o.refs || []) {
      const y = Math.round(yAt(ref.value)) + 0.5;
      ctx.strokeStyle = css(ref.colorVar || '--ink-2');
      ctx.lineWidth = 1.5 * d;
      ctx.setLineDash([5 * d, 4 * d]);
      ctx.beginPath();
      ctx.moveTo(r0.left, y);
      ctx.lineTo(r0.left + r0.pw, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = css(ref.colorVar || '--ink-2');
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(ref.label, r0.left + 4 * d, y - 3 * d);
    }
  }

  VG.LineChart = LineChart;
  VG.drawWeekly = drawWeekly;
})();
