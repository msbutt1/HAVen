/*
 * Overview screen: KPI tiles and charts over the saved recordings and perception tests, the same items the
 * History table lists (so the two always agree). Drawn with the vendored Chart.js 4.4.1 (vendor/chartjs).
 *
 * What it does not do: draw a safe or unsafe line, or colour a change as good or bad. There are no validated
 * limits behind these numbers (vibration is unweighted, grip effort is relative to each wearer's calibration,
 * the exposure score is experimental), so deltas are shown in neutral ink and no chart has a "risk zone".
 *
 * Colours: the --ov-* tokens in css/style.css, checked with the dataviz palette validator in light and dark.
 * A source keeps its colour on every chart (live green, replay blue, simulated violet, as the tags).
 */
(function () {
  'use strict';
  const VG = (window.VG = window.VG || {});
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  const SOURCES = ['live', 'replay', 'simulated'];
  const SOURCE_LABEL = { live: 'Live', replay: 'Replay', simulated: 'Simulated' };
  const SOURCE_SLOT = { live: 'green', replay: 'blue', simulated: 'violet' };
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  // ---------- small helpers ----------
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const sum = (a) => a.reduce((s, v) => s + v, 0);
  const avg = (a) => (a.length ? sum(a) / a.length : null);
  const fmt = (v, dp = 0) => (isNum(v) ? v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp }) : '—');
  const startOfDay = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const dayLabel = (t) => new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
  const stampLabel = (t) => new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  /** seconds -> [number text, unit] */
  function duration(s) {
    if (!isNum(s)) return ['—', ''];
    if (s < 60) return [fmt(s, 0), 's'];
    if (s < 3600) return [fmt(s / 60, 0), 'min'];
    return [fmt(s / 3600, 1), 'h'];
  }
  const durationText = (s) => duration(s).join(' ').trim();

  // Chart.js parses colours itself (for hover shades) and does not read OKLCH, so each token is turned into
  // rgb() by painting it on a 1x1 canvas.
  const probe = document.createElement('canvas');
  probe.width = probe.height = 1;
  const pctx = probe.getContext('2d', { willReadFrequently: true });
  function rgb(css) {
    pctx.clearRect(0, 0, 1, 1);
    pctx.fillStyle = '#000';
    pctx.fillStyle = css;
    pctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = pctx.getImageData(0, 0, 1, 1).data;
    return a === 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
  }
  function tokens() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name) => rgb(cs.getPropertyValue(name).trim() || '#888');
    return {
      ink: v('--foreground'), muted: v('--muted-foreground'), grid: v('--border'), surface: v('--card'),
      blue: v('--ov-blue'), blue2: v('--ov-blue-2'), orange: v('--ov-orange'), teal: v('--ov-teal'),
      green: v('--ov-green'), violet: v('--ov-violet'), neutral: v('--ov-neutral'), track: v('--ov-track'),
      font: cs.getPropertyValue('--sans').trim() || 'system-ui, sans-serif',
    };
  }

  // ---------- data ----------
  function prepare(items) {
    const sessions = [];
    const tests = [];
    for (const it of items) {
      const t = Date.parse(it.at);
      if (!Number.isFinite(t)) continue;
      const r = it.rec;
      const src = SOURCES.includes(r.provenance) ? r.provenance : 'simulated';
      if (it.kind === 'session') {
        sessions.push({ t, src, sample: !!r.sample, dur: isNum(r.duration_s) ? r.duration_s : 0,
          score: isNum(r.score_total) ? r.score_total : null, mean: isNum(r.mean_activation_pct) ? r.mean_activation_pct : null,
          peak: isNum(r.peak_activation_pct) ? r.peak_activation_pct : null, vib: isNum(r.peak_vib_ahv_est) ? r.peak_vib_ahv_est : null,
          tool: isNum(r.tool_time_s) ? r.tool_time_s : null });
      } else {
        const trials = r.trials || [];
        tests.push({ t, src, sample: !!r.sample, threshold: isNum(r.threshold_setting) ? r.threshold_setting : null,
          felt: trials.filter((x) => x.response === 'felt').length,
          notFelt: trials.filter((x) => x.response === 'not_felt').length,
          missing: trials.filter((x) => x.response === 'missing').length });
      }
    }
    sessions.sort((a, b) => a.t - b.t);
    tests.sort((a, b) => a.t - b.t);
    return { sessions, tests };
  }

  function sliceOf(data, from, to, source) {
    const keep = (x) => x.t >= from && x.t < to && (source === 'all' || x.src === source);
    return { sessions: data.sessions.filter(keep), tests: data.tests.filter(keep) };
  }

  function stats(s) {
    const withTool = s.sessions.filter((x) => x.tool != null);
    const toolBase = sum(withTool.map((x) => x.dur));
    const vibs = s.sessions.map((x) => x.vib).filter(isNum);
    const lastTest = [...s.tests].reverse().find((x) => x.threshold != null);
    return {
      n: s.sessions.length,
      recS: sum(s.sessions.map((x) => x.dur)),
      toolS: withTool.length ? sum(withTool.map((x) => x.tool)) : null,
      toolShare: toolBase > 0 ? sum(withTool.map((x) => x.tool)) / toolBase : null,
      peakVib: vibs.length ? Math.max(...vibs) : null,
      grip: avg(s.sessions.map((x) => x.mean).filter(isNum)),
      tests: s.tests.length,
      lastThreshold: lastTest ? lastTest.threshold : null,
    };
  }

  // ---------- KPI tiles ----------
  const KPIS = [
    { key: 'n', label: 'Recordings', value: (v) => [fmt(v), ''] },
    { key: 'recS', label: 'Recorded time', value: duration },
    { key: 'toolS', label: 'Tool time (estimated)', value: duration,
      extra: (c) => (c.toolShare == null ? 'not measured in these recordings' : `${fmt(c.toolShare * 100)} % of recorded time`) },
    { key: 'peakVib', label: 'Highest peak vibration', value: (v) => [fmt(v, 1), 'm/s²'], extra: () => 'unweighted, not ISO 5349' },
    { key: 'grip', label: 'Mean grip effort', value: (v) => [fmt(v), '%'], extra: () => 'of each wearer’s calibration' },
    { key: 'tests', label: 'Perception tests', value: (v) => [fmt(v), ''],
      extra: (c) => (c.lastThreshold == null ? 'no threshold yet' : `latest threshold: setting ${fmt(c.lastThreshold)}`) },
  ];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function renderKpis(cur, prev, periodText) {
    const box = $('kpis');
    box.textContent = '';
    for (const k of KPIS) {
      const tile = el('article', 'kpi');
      tile.appendChild(el('h4', null, k.label));
      const [num, unit] = k.value(cur[k.key]);
      const kv = el('p', 'kv', num);
      if (unit && num !== '—') kv.appendChild(el('small', null, unit));
      tile.appendChild(kv);
      if (prev) {
        const kd = el('div', 'kd');
        const c = cur[k.key];
        const p = prev[k.key];
        let change = '—';
        if (isNum(c) && isNum(p)) {
          if (p === 0) change = c === 0 ? '± 0 %' : 'new';
          else {
            const pct = ((c - p) / Math.abs(p)) * 100;
            change = Math.abs(pct) < 0.5 ? '± 0 %' : `${pct > 0 ? '▲' : '▼'} ${fmt(Math.abs(pct))} %`;
          }
        }
        const a = el('span'); a.appendChild(el('b', null, change)); a.appendChild(document.createTextNode('change'));
        const [pn, pu] = k.value(p);
        const b = el('span'); b.appendChild(el('b', null, pn === '—' ? '—' : `${pn}${pu ? ` ${pu}` : ''}`)); b.appendChild(document.createTextNode(periodText));
        kd.append(a, b);
        tile.appendChild(kd);
      }
      if (k.extra) tile.appendChild(el('p', 'kx', k.extra(cur)));
      box.appendChild(tile);
    }
  }

  // ---------- Chart.js setup ----------
  const charts = {};
  let T = null;

  // A vertical hairline at the hovered x on line charts (the tooltip lists every series there).
  const crosshair = {
    id: 'ovCrosshair',
    afterDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      const x = active[0].element.x;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = T.muted;
      ctx.globalAlpha = 0.5;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, top);
      ctx.lineTo(Math.round(x) + 0.5, bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  // Value at the tip of each horizontal bar (ranked bars), in muted ink, never inside the bar.
  const tipLabels = {
    id: 'ovTipLabels',
    afterDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const ctx = chart.ctx;
      const meta = chart.getDatasetMeta(0);
      const data = chart.data.datasets[0].data;
      ctx.save();
      ctx.fillStyle = T.muted;
      ctx.font = `500 12px ${T.font}`;
      ctx.textBaseline = 'middle';
      meta.data.forEach((bar, i) => {
        if (!isNum(data[i])) return;
        ctx.fillText(opts.format(data[i]), bar.x + 6, bar.y);
      });
      ctx.restore();
    },
  };

  function base(extra = {}) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion.matches ? false : { duration: 350 },
      layout: { padding: { top: 4, right: 8 } },
      plugins: {
        legend: { display: false, position: 'bottom', labels: { color: T.ink, boxWidth: 10, boxHeight: 10, usePointStyle: true, padding: 14 } },
        tooltip: {
          backgroundColor: T.surface, titleColor: T.muted, bodyColor: T.ink, borderColor: T.grid, borderWidth: 1,
          padding: 10, cornerRadius: 8, usePointStyle: true, boxPadding: 4,
          titleFont: { family: T.font, weight: '500', size: 12 }, bodyFont: { family: T.font, weight: '600', size: 13 },
        },
        ovCrosshair: { enabled: false },
        ovTipLabels: { enabled: false },
      },
      ...extra,
    };
  }
  function seriesColor(dataset, i) {
    if (Array.isArray(dataset.backgroundColor)) return dataset.backgroundColor[i];
    return dataset.type === 'line' || dataset.showLine ? dataset.borderColor : (dataset._key || dataset.backgroundColor);
  }
  /** Tooltip: value first, series second; keyed with the series colour (a line for lines, a square for the rest). */
  function tip(kind, callbacks) {
    return {
      ...base().plugins.tooltip,
      callbacks: {
        labelColor: (c) => { const col = seriesColor(c.dataset, c.dataIndex); return { borderColor: col, backgroundColor: col, borderWidth: 2 }; },
        labelPointStyle: () => ({ pointStyle: kind === 'line' ? 'line' : kind === 'dot' ? 'circle' : 'rect', rotation: 0 }),
        ...callbacks,
      },
    };
  }
  /** Legend, keyed the same way (Chart.js would draw line keys in the point ring colour). */
  function legend(kind, show = true) {
    const l = base().plugins.legend;
    return {
      ...l, display: show,
      labels: {
        ...l.labels,
        generateLabels: (chart) => {
          const doughnut = chart.config.type === 'doughnut';
          const names = doughnut ? chart.data.labels : chart.data.datasets.map((d) => d.label);
          return names.map((text, i) => {
            const col = doughnut ? chart.data.datasets[0].backgroundColor[i] : seriesColor(chart.data.datasets[i], 0);
            const hidden = doughnut ? !chart.getDataVisibility(i) : !chart.isDatasetVisible(i);
            return { text, fillStyle: col, strokeStyle: col, lineWidth: kind === 'line' ? 2 : 0, fontColor: T.ink, hidden,
              pointStyle: kind === 'line' ? 'line' : kind === 'dot' ? 'circle' : 'rect', datasetIndex: doughnut ? 0 : i, index: i };
          });
        },
      },
    };
  }
  const gridAxis = (extra = {}) => ({
    grid: { color: T.grid, drawTicks: false }, border: { display: false },
    ticks: { color: T.muted, padding: 6, font: { family: T.font, size: 11 } }, ...extra,
  });
  const plainAxis = (extra = {}) => ({
    grid: { display: false }, border: { color: T.grid },
    ticks: { color: T.muted, padding: 6, font: { family: T.font, size: 11 }, autoSkipPadding: 12, maxRotation: 0 }, ...extra,
  });
  const axisTitle = (text) => ({ display: true, text, color: T.muted, font: { family: T.font, size: 11 } });

  function draw(id, config, hasData, emptyText) {
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    const canvas = $(id);
    const plot = canvas.parentElement;
    let empty = plot.querySelector('.ov-empty');
    if (!hasData) {
      if (!empty) { empty = el('div', 'ov-empty'); plot.appendChild(empty); }
      empty.textContent = emptyText;
      empty.hidden = false;
      canvas.hidden = true;
      const center = plot.querySelector('.ov-center');
      if (center) center.textContent = '';
      return;
    }
    if (empty) empty.hidden = true;
    canvas.hidden = false;
    charts[id] = new window.Chart(canvas, { ...config, plugins: [crosshair, tipLabels] });
  }

  /** The numbers behind a chart, as a table under it (tooltips never gate a value). */
  function numbers(id, headers, rows) {
    const fig = $(id).closest('figure');
    let det = fig.querySelector('.ov-numbers');
    if (!det) {
      det = el('details', 'ov-numbers');
      det.appendChild(el('summary', null, 'Numbers'));
      const wrap = el('div', 'ov-tablewrap');
      wrap.appendChild(el('table'));
      det.appendChild(wrap);
      fig.appendChild(det);
    }
    det.hidden = rows.length === 0;
    const table = det.querySelector('table');
    table.textContent = '';
    const thead = el('thead');
    const hr = el('tr');
    headers.forEach((h) => hr.appendChild(el('th', null, h)));
    thead.appendChild(hr);
    const tbody = el('tbody');
    for (const r of rows) {
      const tr = el('tr');
      r.forEach((c) => tr.appendChild(el('td', null, c)));
      tbody.appendChild(tr);
    }
    table.append(thead, tbody);
  }

  // Bar ends: 4px rounded data-end, square at the baseline; on a stack only the outermost segment is rounded,
  // and every segment gives up 2px in the surface colour as the gap to its neighbour.
  function stackEnd(horizontal) {
    return (ctx) => {
      const ds = ctx.chart.data.datasets;
      let top = -1;
      ds.forEach((d, j) => { if (isNum(d.data[ctx.dataIndex]) && d.data[ctx.dataIndex] > 0 && ctx.chart.isDatasetVisible(j)) top = j; });
      if (ctx.datasetIndex !== top) return 0;
      return horizontal ? { topRight: 4, bottomRight: 4 } : { topLeft: 4, topRight: 4 };
    };
  }

  // ---------- the charts ----------
  function perDay(s, from, to) {
    const span = Math.round((to - from) / DAY);
    const step = span > 70 ? 7 : 1;
    const bins = [];
    for (let t = from; t < to; t += step * DAY) bins.push(t);
    const idx = (t) => Math.min(bins.length - 1, Math.floor((t - from) / (step * DAY)));
    const counts = Object.fromEntries(SOURCES.map((k) => [k, bins.map(() => 0)]));
    s.sessions.forEach((x) => { counts[x.src][idx(x.t)] += 1; });
    const present = SOURCES.filter((k) => counts[k].some((v) => v > 0));
    draw('ov-per-day', {
      type: 'bar',
      data: {
        labels: bins.map((t) => (step === 7 ? `Week of ${dayLabel(t)}` : dayLabel(t))),
        datasets: present.map((k) => ({
          label: SOURCE_LABEL[k], data: counts[k].map((v) => v || null), backgroundColor: T[SOURCE_SLOT[k]],
          borderColor: T.surface, borderWidth: { top: 2, right: 0, bottom: 0, left: 0 }, borderSkipped: 'start',
          borderRadius: stackEnd(false), maxBarThickness: 24, categoryPercentage: 0.8, barPercentage: 0.9, pointStyle: 'rect',
        })),
      },
      options: base({
        interaction: { mode: 'index', intersect: false },
        scales: { x: plainAxis({ stacked: true }), y: gridAxis({ stacked: true, beginAtZero: true, ticks: { ...gridAxis().ticks, precision: 0 } }) },
        plugins: { ...base().plugins, legend: legend('bar', present.length > 1),
          tooltip: { ...tip('bar', { label: (c) => ` ${c.raw} ${c.raw === 1 ? 'recording' : 'recordings'}  ${c.dataset.label}` }), filter: (i) => isNum(i.raw) } },
      }),
    }, s.sessions.length > 0, 'No recordings in this period.');
    numbers('ov-per-day', [step === 7 ? 'Week of' : 'Day', ...present.map((k) => SOURCE_LABEL[k])],
      bins.map((t, i) => [dayLabel(t), ...present.map((k) => String(counts[k][i]))]).filter((r) => r.slice(1).some((v) => v !== '0')));
  }

  function gripTrend(s) {
    const rows = s.sessions.filter((x) => x.mean != null);
    const last = rows.length - 1;
    const endDot = (ctx) => (ctx.dataIndex === last ? 4 : 0);
    const line = (label, key, color) => ({
      type: 'line', label, data: rows.map((x) => (x[key] == null ? null : Math.round(x[key] * 10) / 10)), borderColor: color, backgroundColor: color,
      borderWidth: 2, tension: 0.3, spanGaps: true, pointRadius: endDot, pointHoverRadius: 5, pointBorderColor: T.surface,
      pointBorderWidth: 2, pointStyle: 'line', borderCapStyle: 'round', borderJoinStyle: 'round',
    });
    const hasPeak = rows.some((x) => x.peak != null);
    const datasets = [line('Mean', 'mean', T.blue)];
    if (hasPeak) datasets.push(line('Peak', 'peak', T.blue2));
    draw('ov-trend', {
      type: 'line',
      data: { labels: rows.map((x) => stampLabel(x.t)), datasets },
      options: base({
        interaction: { mode: 'index', intersect: false },
        scales: { x: plainAxis({ ticks: { ...plainAxis().ticks, callback(v) { return this.getLabelForValue(v).split(',')[0]; } } }),
          y: gridAxis({ beginAtZero: true, suggestedMax: 100, ticks: { ...gridAxis().ticks, callback: (v) => `${v} %` } }) },
        plugins: { ...base().plugins, legend: legend('line', datasets.length > 1), ovCrosshair: { enabled: true },
          tooltip: tip('line', { label: (c) => ` ${fmt(c.raw)} %  ${c.dataset.label}` }) },
      }),
    }, rows.length > 0, 'No calibrated recordings in this period (grip effort needs a calibration).');
    numbers('ov-trend', ['Recording', 'Mean %', 'Peak %'], rows.map((x) => [stampLabel(x.t), fmt(x.mean), fmt(x.peak)]).reverse());
  }

  function topScores(s) {
    const rows = s.sessions.filter((x) => x.score != null).sort((a, b) => b.score - a.score).slice(0, 8);
    const dp = rows.some((x) => x.score < 10) ? 2 : 1;
    draw('ov-score', {
      type: 'bar',
      data: { labels: rows.map((x) => stampLabel(x.t)), datasets: [{ label: 'Exposure score', data: rows.map((x) => x.score),
        backgroundColor: T.teal, borderRadius: 4, borderSkipped: 'start', maxBarThickness: 18, categoryPercentage: 0.8, barPercentage: 0.9 }] },
      options: base({
        indexAxis: 'y',
        layout: { padding: { top: 4, right: 44 } },
        scales: { x: gridAxis({ beginAtZero: true, title: axisTitle('au (arbitrary units)') }), y: plainAxis({ ticks: { ...plainAxis().ticks, autoSkip: false } }) },
        plugins: { ...base().plugins, ovTipLabels: { enabled: true, format: (v) => fmt(v, dp) },
          tooltip: tip('bar', { label: (c) => ` ${fmt(c.raw, dp)} au  experimental score` }) },
      }),
    }, rows.length > 0, 'No scored recordings in this period (the score needs a calibration).');
    numbers('ov-score', ['Recording', 'Score (au)', 'Length'], rows.map((x) => [stampLabel(x.t), fmt(x.score, dp), durationText(x.dur)]));
  }

  function toolDonut(s, st) {
    const rows = s.sessions.filter((x) => x.tool != null);
    const base0 = sum(rows.map((x) => x.dur));
    const tool = Math.min(base0, sum(rows.map((x) => x.tool)));
    draw('ov-tool', {
      type: 'doughnut',
      data: { labels: ['Tool running (estimated)', 'No tool detected'],
        datasets: [{ data: [tool, Math.max(0, base0 - tool)], backgroundColor: [T.orange, T.track], borderColor: T.surface, borderWidth: 2, hoverOffset: 2 }] },
      options: base({
        cutout: '74%',
        plugins: { ...base().plugins, legend: legend('bar'),
          tooltip: tip('bar', { label: (c) => ` ${durationText(c.raw)}  ${c.label}` }) },
      }),
    }, base0 > 0, 'No tool-use data in this period.');
    const center = $('ov-tool-center');
    center.textContent = '';
    if (base0 > 0) {
      center.appendChild(el('b', null, `${fmt(st.toolShare * 100)} %`));
      center.appendChild(document.createTextNode('of recorded time'));
    }
    numbers('ov-tool', ['', 'Time'], base0 > 0 ? [['Tool running (estimated)', durationText(tool)], ['No tool detected', durationText(base0 - tool)], ['Recorded (with tool data)', durationText(base0)]] : []);
  }

  function answers(s) {
    const rows = s.tests.slice(-8);
    const set = (label, key, color) => ({
      label, data: rows.map((x) => x[key] || null), backgroundColor: color, borderColor: T.surface,
      borderWidth: { top: 0, right: 2, bottom: 0, left: 0 }, borderSkipped: 'start', borderRadius: stackEnd(true),
      maxBarThickness: 18, categoryPercentage: 0.8, barPercentage: 0.9, pointStyle: 'rect',
    });
    draw('ov-answers', {
      type: 'bar',
      data: { labels: rows.map((x) => stampLabel(x.t)),
        datasets: [set('Felt it', 'felt', T.blue), set('Didn’t feel it', 'notFelt', T.orange), set('No response', 'missing', T.neutral)] },
      options: base({
        indexAxis: 'y',
        interaction: { mode: 'index', intersect: false, axis: 'y' },
        scales: { x: gridAxis({ stacked: true, beginAtZero: true, ticks: { ...gridAxis().ticks, precision: 0 }, title: axisTitle('trials') }),
          y: plainAxis({ stacked: true, ticks: { ...plainAxis().ticks, autoSkip: false } }) },
        plugins: { ...base().plugins, legend: legend('bar'),
          tooltip: { ...tip('bar', { label: (c) => ` ${c.raw}  ${c.dataset.label}` }), filter: (i) => isNum(i.raw) } },
      }),
    }, rows.length > 0, 'No perception tests in this period.');
    numbers('ov-answers', ['Test', 'Felt', 'Not felt', 'No response', 'Threshold'],
      rows.map((x) => [stampLabel(x.t), String(x.felt), String(x.notFelt), String(x.missing), x.threshold == null ? '—' : fmt(x.threshold)]).reverse());
  }

  function scatter(s) {
    const rows = s.sessions.filter((x) => x.vib != null && x.mean != null);
    const present = SOURCES.filter((k) => rows.some((x) => x.src === k));
    draw('ov-scatter', {
      type: 'scatter',
      data: { datasets: present.map((k) => ({
        label: SOURCE_LABEL[k], data: rows.filter((x) => x.src === k).map((x) => ({ x: x.vib, y: x.mean, t: x.t })),
        backgroundColor: T[SOURCE_SLOT[k]], _key: T[SOURCE_SLOT[k]], borderColor: T.surface, borderWidth: 2, pointRadius: 5, pointHoverRadius: 7, pointHitRadius: 12,
      })) },
      options: base({
        interaction: { mode: 'nearest', intersect: false },
        scales: { x: gridAxis({ beginAtZero: true, title: axisTitle('Peak vibration (m/s²)') }),
          y: gridAxis({ beginAtZero: true, suggestedMax: 100, title: axisTitle('Mean grip effort (%)'), ticks: { ...gridAxis().ticks, callback: (v) => `${v}` } }) },
        plugins: { ...base().plugins, legend: legend('dot', present.length > 1),
          tooltip: tip('dot', {
            title: (items) => (items.length ? stampLabel(items[0].raw.t) : ''),
            label: (c) => ` ${fmt(c.raw.x, 2)} m/s² · ${fmt(c.raw.y)} %  ${c.dataset.label}` }) },
      }),
    }, rows.length > 0, 'No recordings with both grip effort and vibration in this period.');
    numbers('ov-scatter', ['Recording', 'Peak vibration (m/s²)', 'Mean grip %', 'Source'],
      rows.map((x) => [stampLabel(x.t), fmt(x.vib, 2), fmt(x.mean), SOURCE_LABEL[x.src]]).reverse());
  }

  function single(id, rows, key, color, unit, dp, empty) {
    const last = rows.length - 1;
    draw(id, {
      type: 'line',
      data: { labels: rows.map((x) => stampLabel(x.t)), datasets: [{ type: 'line', label: unit, data: rows.map((x) => x[key]), borderColor: color, backgroundColor: color,
        borderWidth: 2, tension: 0.3, pointRadius: (c) => (c.dataIndex === last || rows.length <= 12 ? 4 : 0), pointHoverRadius: 5,
        pointBorderColor: T.surface, pointBorderWidth: 2, pointStyle: 'circle' }] },
      options: base({
        interaction: { mode: 'index', intersect: false },
        scales: { x: plainAxis({ ticks: { ...plainAxis().ticks, callback(v) { return this.getLabelForValue(v).split(',')[0]; } } }),
          y: gridAxis({ beginAtZero: true }) },
        plugins: { ...base().plugins, ovCrosshair: { enabled: true },
          tooltip: tip('line', { label: (c) => ` ${fmt(c.raw, dp)} ${unit}` }) },
      }),
    }, rows.length > 0, empty);
  }

  function vibTrend(s) {
    const rows = s.sessions.filter((x) => x.vib != null);
    single('ov-vib', rows, 'vib', T.orange, 'm/s²', 2, 'No vibration readings in this period.');
    numbers('ov-vib', ['Recording', 'Peak (m/s²)'], rows.map((x) => [stampLabel(x.t), fmt(x.vib, 2)]).reverse());
  }

  function thresholds(s) {
    const rows = s.tests.filter((x) => x.threshold != null);
    single('ov-threshold', rows, 'threshold', T.blue, 'setting', 0, 'No perception thresholds in this period.');
    numbers('ov-threshold', ['Test', 'Threshold setting'], rows.map((x) => [stampLabel(x.t), fmt(x.threshold)]).reverse());
  }

  function sources(s) {
    const counts = SOURCES.map((k) => s.sessions.filter((x) => x.src === k).length + s.tests.filter((x) => x.src === k).length);
    const present = SOURCES.filter((_, i) => counts[i] > 0);
    const total = sum(counts);
    draw('ov-sources', {
      type: 'doughnut',
      data: { labels: present.map((k) => SOURCE_LABEL[k]),
        datasets: [{ data: present.map((k) => counts[SOURCES.indexOf(k)]), backgroundColor: present.map((k) => T[SOURCE_SLOT[k]]),
          borderColor: T.surface, borderWidth: 2, hoverOffset: 2 }] },
      options: base({
        cutout: '74%',
        plugins: { ...base().plugins, legend: legend('bar'),
          tooltip: tip('bar', { label: (c) => ` ${c.raw} ${c.raw === 1 ? 'item' : 'items'}  ${c.label}` }) },
      }),
    }, total > 0, 'Nothing saved in this period.');
    const center = $('ov-sources-center');
    center.textContent = '';
    if (total > 0) {
      center.appendChild(el('b', null, String(total)));
      center.appendChild(document.createTextNode(total === 1 ? 'saved item' : 'saved items'));
    }
    numbers('ov-sources', ['Source', 'Recordings', 'Tests'], present.map((k) => [SOURCE_LABEL[k],
      String(s.sessions.filter((x) => x.src === k).length), String(s.tests.filter((x) => x.src === k).length)]));
  }

  // ---------- the screen ----------
  let lastItems = [];

  function render(items) {
    if (items) lastItems = items;
    if (!window.Chart) return;
    T = tokens();
    window.Chart.defaults.font.family = T.font;
    window.Chart.defaults.color = T.muted;
    window.Chart.defaults.borderColor = T.grid;

    const data = prepare(lastItems);
    const days = Number($('ov-period').value);
    const source = $('ov-source').value;
    const now = Date.now();
    let from;
    let to;
    let prev = null;
    let periodText = '';
    if (days > 0) {
      to = startOfDay(now) + DAY;
      from = to - days * DAY;
      prev = stats(sliceOf(data, from - days * DAY, from, source));
      periodText = `previous ${days} days`;
    } else {
      const first = Math.min(...[...data.sessions, ...data.tests].map((x) => x.t), now);
      from = startOfDay(first);
      to = startOfDay(now) + DAY;
    }
    const s = sliceOf(data, from, to, source);
    const cur = stats(s);

    const all = [...s.sessions, ...s.tests];
    const simulated = all.filter((x) => x.src === 'simulated').length;
    const sample = all.some((x) => x.sample);
    const scope = $('ov-scope');
    scope.textContent = all.length
      ? `${cur.n} ${cur.n === 1 ? 'recording' : 'recordings'} and ${cur.tests} perception ${cur.tests === 1 ? 'test' : 'tests'}, ${days > 0 ? `${dayLabel(from)} to ${dayLabel(to - DAY)}` : 'all time'}. Same items as the History screen.`
      : 'Nothing saved in this period. Record a session on the Live screen or run a perception test.';
    const band = $('ov-sim');
    band.hidden = simulated === 0;
    band.textContent = simulated === all.length
      ? (sample ? 'Simulated sample history, made up on this device so this screen has something to show. None of it is a measurement from a person.'
        : 'All of this is simulated data, not measurements from a person.')
      : `${simulated} of ${all.length} items here are simulated, not measurements from a person.`;

    renderKpis(cur, prev, periodText);
    perDay(s, from, to);
    gripTrend(s);
    topScores(s);
    toolDonut(s, cur);
    answers(s);
    scatter(s);
    vibTrend(s);
    thresholds(s);
    sources(s);
  }

  function isShown() { const v = $('view-overview'); return v && !v.hidden; }

  $('ov-period').addEventListener('change', () => render());
  $('ov-source').addEventListener('change', () => render());
  // the charts carry resolved colours, so a theme switch redraws them
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (isShown()) render(); });

  VG.Overview = { render, isShown, _prepare: prepare, _stats: stats };
})();
