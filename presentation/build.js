// HAVen pitch deck, rebuilt in the app's own visual language (enamel dark and enamel light).
// Content and speaker notes are carried over from the previous deck unchanged.
const P = require('pptxgenjs');
const fs = require('fs');
const path = require('path');
const pres = new P();
pres.layout = 'LAYOUT_WIDE';            // 13.333 x 7.5 in
pres.title = 'HAVen';
pres.author = 'HAVen team';
const NOTES = JSON.parse(fs.readFileSync(path.join(__dirname, 'notes.json'), 'utf8'));
const AS = (f) => path.join(__dirname, 'assets', f);
const HEAD = 'Onest', MONO = 'JetBrains Mono';

// the app's enamel palette (index.html, enamel dark and enamel light)
const DARK = { bg: '0E2B20', card: '143528', card2: '1B3D2F', rim: '05140D', hair: '2B5A47', text: 'EEF4F0', muted: 'A6BAB0',
               accent: '5FC597', amber: 'F0A643', red: 'FF7670', low: '57C787', pill: '1B3D2F', tile: '24513D', dark: true };
const LIGHT = { bg: 'CFE6D7', card: 'FFFFFF', card2: 'EAF4EE', rim: '18211D', hair: '9CC4AE', text: '18211D', muted: '45544D',
                accent: '1D5A45', amber: 'B86E00', red: 'CF3434', low: '2F9A5B', pill: 'FFFFFF', tile: 'D5EADF', dark: false };
const SLEEVE_TAGS = { ink: '18211D' };

let n = 0;
function slide(th, notesIdx) {
  n += 1;
  const s = pres.addSlide();
  s.background = { color: th.bg };
  if (notesIdx !== undefined && NOTES[notesIdx]) s.addNotes(NOTES[notesIdx]);
  return s;
}
function pill(s, th, label, x = 0.7, y = 0.55) {
  const w = Math.max(1.9, label.length * 0.122 + 0.6);
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.36, rectRadius: 0.18, fill: { color: th.pill },
    line: { color: th.hair, width: 0.75 } });
  s.addShape(pres.shapes.OVAL, { x: x + 0.17, y: y + 0.13, w: 0.1, h: 0.1, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addText(label.toUpperCase(), { x: x + 0.36, y: y + 0.04, w: w - 0.4, h: 0.28, fontFace: MONO, fontSize: 10.5,
    color: th.accent, bold: true, charSpacing: 1.2, margin: 0, valign: 'middle' });
}
function title(s, th, text, o = {}) {
  s.addText(text, { x: o.x ?? 0.7, y: o.y ?? 1.05, w: o.w ?? 11.9, h: o.h ?? 1.3, fontFace: HEAD, fontSize: o.size ?? 38,
    bold: true, color: o.color ?? th.text, margin: 0, valign: 'top', charSpacing: -0.4, lineSpacingMultiple: 0.95 });
}
function footer(s, th, text, num) {
  if (text) s.addText(text, { x: 0.7, y: 6.82, w: 10.8, h: 0.3, fontFace: MONO, fontSize: 9.5, color: th.muted, margin: 0 });
  s.addText(String(num).padStart(2, '0'), { x: 12.0, y: 6.82, w: 0.65, h: 0.3, fontFace: MONO, fontSize: 9.5, color: th.muted,
    align: 'right', margin: 0 });
}
function card(s, th, x, y, w, h, o = {}) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: o.r ?? 0.22, fill: { color: o.fill ?? th.card },
    line: { color: o.rim ?? (th.dark ? th.hair : th.rim), width: o.rimW ?? (th.dark ? 0.75 : 2.25) } });
}
function tile(s, th, x, y, size, icon, tint) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: size, h: size, rectRadius: size * 0.3, fill: { color: tint ?? th.tile },
    line: { color: tint ?? th.tile, width: 0 } });
  const g = size * 0.56;
  s.addImage({ path: AS(`ico_${icon}_${th.dark ? 'mint' : 'green'}.png`), x: x + (size - g) / 2, y: y + (size - g) / 2, w: g, h: g });
}
// a phone with a real app screen: outer body, rounded screen (the screenshot is pre-rounded)
function phone(s, th, file, x, y, h) {
  const w = h * 0.4615 + 0.18;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: w * 0.2, fill: { color: '04110B' },
    line: { color: '3A6B56', width: 1.25 } });
  const sw = w - 0.18, sh = sw / 0.4615;
  s.addImage({ path: AS(file), x: x + 0.09, y: y + (h - sh) / 2, w: sw, h: sh });
  return w;
}
function scaleBar(s, th, x, y, w, h) {
  const stops = [[0, [87, 199, 135]], [0.25, [240, 166, 67]], [0.6, [255, 138, 76]], [1, [255, 112, 118]]];
  const N = 90;
  for (let k = 0; k < N; k++) {
    const t = (k + 0.5) / N;
    let c = stops[stops.length - 1][1];
    for (let j = 0; j < stops.length - 1; j++) {
      const [p0, a] = stops[j], [p1, b] = stops[j + 1];
      if (t >= p0 && t <= p1) { const u = (t - p0) / (p1 - p0); c = a.map((v, i) => Math.round(v + (b[i] - v) * u)); break; }
    }
    const hex = c.map((v) => v.toString(16).padStart(2, '0')).join('').toUpperCase();
    s.addShape(pres.shapes.RECTANGLE, { x: x + (w * k) / N, y, w: w / N + 0.012, h, fill: { color: hex }, line: { color: hex, width: 0 } });
  }
}

// ----------------------------------------------------------------- 1 · title (dark)
{
  const th = DARK, s = slide(th, 0);
  pill(s, th, 'NATHACKS 2026 · CONSTRUCTION & MANUFACTURING', 0.7, 0.7);
  s.addText('HAVen', { x: 0.62, y: 1.9, w: 7.6, h: 2.0, fontFace: HEAD, fontSize: 130, bold: true, color: th.text, margin: 0, charSpacing: -3 });
  s.addText('A forearm sleeve that tracks how much vibration a worker’s hands really take, and warns them before it becomes permanent damage.',
    { x: 0.7, y: 4.25, w: 6.4, h: 1.2, fontFace: HEAD, fontSize: 20, color: th.muted, margin: 0, lineSpacingMultiple: 1.25 });
  s.addText('[Team name] · University of Alberta', { x: 0.7, y: 6.6, w: 6.5, h: 0.3, fontFace: MONO, fontSize: 11, color: th.muted, margin: 0 });
  phone(s, th, 'screen_today-dark.png', 8.35, 0.7, 6.1);
  phone(s, th, 'screen_overview-light.png', 10.55, 1.6, 5.1);
}

// ----------------------------------------------------------------- 2 · team (light)
{
  const th = LIGHT, s = slide(th, 1);
  pill(s, th, 'The team', 0.7, 0.55);
  title(s, th, 'Five builders, one sleeve', { y: 1.05, h: 0.8 });
  const people = [
    ['Fahad', 'Exposure engine · frontend', 'CS 3rd Year', 'team_fahad.png'],
    ['Salaar', 'Firmware · signals', 'CS 4th Year', 'team_salaar.png'],
    ['Abduljabbar', 'Backend · app', '', 'team_abduljabbar.png'],
    ['Ali', 'CAD · parts', 'CS 2nd Year', null],
    ['Collins', 'Hardware · sleeve', 'Electrical Eng 2nd Year', 'team_collins.png'],
  ];
  const CW = 2.23, G = 0.2;
  people.forEach(([name, role, prog, img], i) => {
    const x = 0.7 + i * (CW + G), y = 2.25;
    card(s, th, x, y, CW, 4.35);
    if (img) s.addImage({ path: AS(img), x: x + 0.22, y: y + 0.22, w: CW - 0.44, h: CW - 0.44 });
    else {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: x + 0.22, y: y + 0.22, w: CW - 0.44, h: CW - 0.44, rectRadius: 0.2, fill: { color: th.tile }, line: { color: th.tile, width: 0 } });
      s.addText(name[0], { x: x + 0.22, y: y + 0.22, w: CW - 0.44, h: CW - 0.44, fontFace: HEAD, fontSize: 64, bold: true, color: th.accent, align: 'center', valign: 'middle', margin: 0 });
    }
    s.addText(name, { x: x + 0.22, y: y + 2.8, w: CW - 0.44, h: 0.4, fontFace: HEAD, fontSize: 17, bold: true, color: th.text, margin: 0 });
    s.addText(role, { x: x + 0.22, y: y + 3.18, w: CW - 0.44, h: 0.5, fontFace: HEAD, fontSize: 14, color: th.muted, margin: 0, valign: 'top' });
    if (prog) s.addText(prog, { x: x + 0.22, y: y + 3.78, w: CW - 0.44, h: 0.3, fontFace: MONO, fontSize: 11, color: th.accent, margin: 0 });
  });
  footer(s, th, '', 2);
}

// ----------------------------------------------------------------- 3 · problem (dark)
{
  const th = DARK, s = slide(th, 2);
  pill(s, th, 'The problem', 0.7, 0.55);
  title(s, th, 'Power tools slowly destroy workers’ hands, and nobody is counting', { h: 1.7, size: 40 });
  const stats = [
    ['Permanent', 'Hand-arm vibration syndrome damages nerves, blood vessels and muscles. There is no cure.'],
    ['266,100', 'people work in Alberta construction (March 2025).'],
    ['No limit', 'Alberta has no numeric vibration limit, and few HAVS claims are accepted in Canada.'],
  ];
  stats.forEach(([big, body], i) => {
    const x = 0.7 + i * 4.1;
    card(s, th, x, 3.25, 3.85, 3.2);
    s.addText(big, { x: x + 0.35, y: 3.6, w: 3.3, h: 0.9, fontFace: HEAD, fontSize: 34, bold: true, color: th.accent, margin: 0, valign: 'middle' });
    s.addText(body, { x: x + 0.35, y: 4.7, w: 3.15, h: 1.5, fontFace: HEAD, fontSize: 16, color: th.text, margin: 0, valign: 'top', lineSpacingMultiple: 1.25 });
  });
  footer(s, th, 'Sources: Job Bank Alberta construction profile; OHS Insider; Journal of the Canadian Acoustical Association', 3);
}

// ----------------------------------------------------------------- 4 · solution (light)
{
  const th = LIGHT, s = slide(th, 3);
  pill(s, th, 'Our solution', 0.7, 0.55);
  phone(s, th, 'screen_today-light.png', 0.95, 1.15, 4.25);
  s.addText('TECH USED · PHONE APP', { x: 0.7, y: 5.6, w: 3.9, h: 0.26, fontFace: MONO, fontSize: 10, bold: true, color: th.accent, margin: 0 });
  card(s, th, 0.7, 5.9, 3.9, 0.86);
  tile(s, th, 0.85, 6.04, 0.58, 'phone');
  s.addText('Java and Swift', { x: 1.6, y: 6.0, w: 2.9, h: 0.3, fontFace: HEAD, fontSize: 12.5, bold: true, color: th.text, margin: 0, valign: 'middle' });
  s.addText('Java on Android, Swift on iPhone, both checked against real recordings.', { x: 1.6, y: 6.3, w: 2.9, h: 0.4, fontFace: HEAD, fontSize: 10, color: th.muted, margin: 0, valign: 'top' });
  s.addText('A sleeve that measures the worker, plus an app that tells them when to stop',
    { x: 5.5, y: 1.2, w: 7.2, h: 1.6, fontFace: HEAD, fontSize: 32, bold: true, color: th.text, margin: 0, valign: 'top', lineSpacingMultiple: 0.98 });
  const items = [
    ['activity', 'Vibration', 'An accelerometer at the wrist measures how hard the tool shakes.'],
    ['lightning', 'Grip effort', 'EMG over the forearm muscles reads how hard the worker squeezes, which a watch can’t reach.'],
    ['clock', 'Real contact time', 'Exposure only counts while the tool is shaking and actually gripped.'],
  ];
  items.forEach(([ic, h, b], i) => {
    const y = 3.2 + i * 1.18;
    card(s, th, 5.5, y, 7.13, 1.02);
    tile(s, th, 5.72, y + 0.2, 0.62, ic);
    s.addText(h, { x: 6.55, y: y + 0.14, w: 5.9, h: 0.36, fontFace: HEAD, fontSize: 18, bold: true, color: th.text, margin: 0 });
    s.addText(b, { x: 6.55, y: y + 0.5, w: 5.95, h: 0.45, fontFace: HEAD, fontSize: 13.5, color: th.muted, margin: 0, valign: 'top' });
  });
  footer(s, th, '', 4);
}

// ----------------------------------------------------------------- 5 · the limit (dark)
{
  const th = DARK, s = slide(th, 4);
  pill(s, th, 'The limit', 0.7, 0.55);
  title(s, th, 'How much vibration is too much?', { h: 0.8, size: 38 });
  // the app's own exposure scale: 0 to 400 points, green through amber to red
  const L = 0.7, W = 11.93, BY = 2.85;
  s.addText('0 points', { x: L, y: 2.45, w: 2, h: 0.28, fontFace: MONO, fontSize: 11, color: th.muted, margin: 0 });
  s.addText('100 · action level', { x: L + W * 0.25 - 1.6, y: 2.45, w: 3.2, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.text, margin: 0, align: 'center' });
  s.addText('400 · daily limit', { x: L + W - 3.2, y: 2.45, w: 3.2, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.text, margin: 0, align: 'right' });
  scaleBar(s, th, L, BY, W, 0.36);
  [0.25, 1].forEach((t) => s.addShape(pres.shapes.RECTANGLE, { x: L + W * t - 0.01, y: BY - 0.12, w: 0.02, h: 0.6, fill: { color: th.text }, line: { color: th.text, width: 0 } }));
  const cards = [
    ['Action level', '100 points', 'Start cutting exposure: rotate tasks, take breaks.'],
    ['Daily limit', '400 points', 'Stop vibrating tool work for the rest of the day.'],
    ['Example: jackhammer', '21 → 83 min', 'Minutes to reach the action level, then the limit.'],
  ];
  cards.forEach(([lab, big, body], i) => {
    const x = L + i * 4.07, y = 3.55;
    card(s, th, x, y, 3.86, 1.75, i === 2 ? { rim: th.accent, rimW: 1.5 } : {});
    s.addText(lab.toUpperCase(), { x: x + 0.28, y: y + 0.2, w: 3.4, h: 0.26, fontFace: MONO, fontSize: 10.5, bold: true, color: th.accent, margin: 0 });
    s.addText(big, { x: x + 0.28, y: y + 0.5, w: 3.4, h: 0.55, fontFace: HEAD, fontSize: 28, bold: true, color: th.text, margin: 0 });
    s.addText(body, { x: x + 0.28, y: y + 1.1, w: 3.35, h: 0.55, fontFace: HEAD, fontSize: 13, color: th.muted, margin: 0, valign: 'top' });
  });
  card(s, th, L, 5.2, W, 1.05);
  s.addText('THE FORMULA · ISO 5349-1 DAILY EXPOSURE', { x: L + 0.3, y: 5.32, w: 8, h: 0.26, fontFace: MONO, fontSize: 10.5, bold: true, color: th.accent, margin: 0 });
  s.addText('A(8) = a × √(T / 8 h)', { x: L + 0.3, y: 5.6, w: 5, h: 0.5, fontFace: HEAD, fontSize: 24, bold: true, color: th.text, margin: 0 });
  s.addText('Points = 100 × (A(8) / 2.5)²', { x: L + 6.0, y: 5.6, w: 5.6, h: 0.5, fontFace: HEAD, fontSize: 24, bold: true, color: th.text, margin: 0 });
  footer(s, th, 'a = vibration at the wrist (m/s²) · T = hours of tool use · 2.5 m/s² = 100 points · 5.0 m/s² = 400 points', 5);
  s.addText('The EU and UK legal values, using the ISO 5349-1 formula. Our wrist sensor is unweighted, so the app gives an estimate, not a certified measurement.',
    { x: 0.7, y: 6.38, w: 11.9, h: 0.42, fontFace: HEAD, fontSize: 12.5, color: th.muted, margin: 0, valign: 'top' });
}

// ----------------------------------------------------------------- 6 · how we built it (light)
{
  const th = LIGHT, s = slide(th, 5);
  pill(s, th, 'How we built it', 0.7, 0.55);
  title(s, th, 'A wireless sleeve and a phone that does all the math', { h: 1.4, size: 36 });
  const boxes = [
    [0.7, 'chip', 'The sleeve', 'ESP32 with an EMG front end on the forearm and an accelerometer at the wrist, streaming over Wi-Fi.'],
    [7.2, 'phone', 'The phone app', 'Runs the whole engine live: filtering, tool-use detection, the ISO 5349 dose and the alarms.'],
  ];
  boxes.forEach(([x, ic, h, b]) => {
    card(s, th, x, 2.5, 5.43, 2.3);
    tile(s, th, x + 0.3, 2.75, 0.8, ic);
    s.addText(h, { x: x + 1.35, y: 2.85, w: 3.8, h: 0.6, fontFace: HEAD, fontSize: 22, bold: true, color: th.text, margin: 0, valign: 'middle' });
    s.addText(b, { x: x + 0.3, y: 3.75, w: 4.85, h: 0.9, fontFace: HEAD, fontSize: 15, color: th.muted, margin: 0, valign: 'top' });
  });
  s.addShape(pres.shapes.RIGHT_ARROW, { x: 6.27, y: 3.15, w: 0.78, h: 0.5, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addText('WI-FI', { x: 6.1, y: 2.75, w: 1.1, h: 0.3, fontFace: MONO, fontSize: 11, bold: true, color: th.accent, align: 'center', margin: 0 });
  // problems we solved: the app's green enamel band
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.7, y: 5.2, w: 11.93, h: 1.0, rectRadius: 0.22, fill: { color: th.accent }, line: { color: th.rim, width: 2.25 } });
  tile(s, th, 0.95, 5.4, 0.6, 'wrench', 'FFFFFF');
  s.addText('Problems we solved', { x: 1.85, y: 5.2, w: 3.6, h: 1.0, fontFace: HEAD, fontSize: 20, bold: true, color: 'FFFFFF', margin: 0, valign: 'middle' });
  s.addText('Telling tool vibration from normal arm movement, and calibrating grip for any wearer.',
    { x: 5.6, y: 5.2, w: 6.7, h: 1.0, fontFace: HEAD, fontSize: 16, color: 'FFFFFF', margin: 0, valign: 'middle' });
  footer(s, th, '', 6);
}

// ----------------------------------------------------------------- 7 · live demo (dark)
{
  const th = DARK, s = slide(th, 6);
  s.addNotes(NOTES[6]);
  pill(s, th, 'The demo · about two minutes', 0.7, 0.55);
  s.addText([{ text: 'Live ', options: { color: th.text } }, { text: 'demo', options: { color: th.accent } }],
    { x: 0.7, y: 1.05, w: 6.1, h: 1.2, fontFace: HEAD, fontSize: 66, bold: true, margin: 0, charSpacing: -1.5 });
  s.addText('One continuous run on the phone and the sleeve. A massage gun stands in for a power tool.',
    { x: 0.7, y: 2.3, w: 5.8, h: 0.6, fontFace: HEAD, fontSize: 16, color: th.muted, margin: 0, valign: 'top' });
  const steps = [
    ['Calibrate', 'Relax, then squeeze. The app learns this wearer’s grip.'],
    ['Tool on', 'Points climb. The ring and sleeve light go green to yellow.'],
    ['Time left', 'Shows the minutes left at this pace, until the action level.'],
    ['Hit 100 points', 'The action-level alarm sounds on the phone.'],
    ['Tool down', 'Counting stops. Only time with the tool running adds points.'],
    ['Overview', 'Week, month and year of history.'],
  ];
  steps.forEach(([h, b], i) => {
    const y = 3.1 + i * 0.6;
    s.addShape(pres.shapes.OVAL, { x: 0.72, y: y + 0.03, w: 0.42, h: 0.42, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
    s.addText(String(i + 1), { x: 0.72, y: y + 0.03, w: 0.42, h: 0.42, fontFace: MONO, fontSize: 13, bold: true, color: '0E2B20', align: 'center', valign: 'middle', margin: 0 });
    s.addText([{ text: h + '  ', options: { bold: true, color: th.text, fontSize: 16 } }, { text: b, options: { color: th.muted, fontSize: 13 } }],
      { x: 1.3, y: y, w: 5.2, h: 0.5, fontFace: HEAD, margin: 0, valign: 'middle' });
  });
  // three real screens from the phone: calibration, the action-level alarm, the history
  phone(s, th, 'screen_setup-intro.png', 6.8, 1.45, 4.3);
  phone(s, th, 'screen_alarm-action.png', 8.95, 1.45, 4.3);
  phone(s, th, 'screen_overview-dark.png', 11.1, 1.45, 4.3);
  footer(s, th, '', 7);
}

// ----------------------------------------------------------------- 8 · over time (dark)
{
  const th = DARK, s = slide(th, 7);
  pill(s, th, 'Over time, not just today', 0.7, 0.55);
  title(s, th, 'From today’s dose to a worker’s long-term risk', { h: 0.6, size: 30 });
  const L = 0.7, W = 11.93, BY = 2.6;
  s.addText('EXPOSURE SCALE · POINTS IN A DAY · YEARS ASSUME EVERY WORKING DAY SITS AT THAT LEVEL', { x: L, y: 2.0, w: W, h: 0.26, fontFace: MONO, fontSize: 10.5, bold: true, color: th.accent, margin: 0 });
  s.addText('0 points', { x: L, y: 2.3, w: 2, h: 0.24, fontFace: MONO, fontSize: 10.5, color: th.muted, margin: 0 });
  s.addText('100 · action level', { x: L + W * 0.25 - 1.6, y: 2.3, w: 3.2, h: 0.24, fontFace: MONO, fontSize: 10.5, bold: true, color: th.text, margin: 0, align: 'center' });
  s.addText('400 · daily limit', { x: L + W - 3.2, y: 2.3, w: 3.2, h: 0.24, fontFace: MONO, fontSize: 10.5, bold: true, color: th.text, margin: 0, align: 'right' });
  scaleBar(s, th, L, BY, W, 0.34);
  s.addText('about 12 years', { x: L + W * 0.25 - 1.6, y: BY + 0.42, w: 3.2, h: 0.36, fontFace: HEAD, fontSize: 18, bold: true, color: th.amber, margin: 0, align: 'center' });
  s.addText('about 6 years', { x: L + W - 3.2, y: BY + 0.42, w: 3.2, h: 0.36, fontFace: HEAD, fontSize: 18, bold: true, color: th.red, margin: 0, align: 'right' });
  const steps = [
    ['Today', 'Today’s points, against the 100 (action level) and 400 (daily limit) lines.', th.low],
    ['Trends', 'The same points charted by week, month and year, so patterns show over time.', 'A6D96A'],
    ['Years to risk', 'About 12 years at the action level, 6 at the limit, until 1 in 10 similar workers get white finger. Not a prediction.', th.amber],
    ['Act early', 'EU and UK advice matched to the worker’s own history. A supervisor can set a lower daily limit.', th.red],
  ];
  const CW = 2.84, G = 0.19;
  steps.forEach(([h, b, col], i) => {
    const x = L + i * (CW + G), y = 4.0;
    card(s, th, x, y, CW, 2.6, { rim: col, rimW: 1.5 });
    s.addShape(pres.shapes.OVAL, { x: x + 0.25, y: y + 0.25, w: 0.42, h: 0.42, fill: { color: col }, line: { color: col, width: 0 } });
    s.addText(String(i + 1), { x: x + 0.25, y: y + 0.25, w: 0.42, h: 0.42, fontFace: MONO, fontSize: 14, bold: true, color: '0E2B20', align: 'center', valign: 'middle', margin: 0 });
    s.addText(h, { x: x + 0.25, y: y + 0.82, w: CW - 0.5, h: 0.36, fontFace: HEAD, fontSize: 18, bold: true, color: th.text, margin: 0 });
    s.addText(b, { x: x + 0.25, y: y + 1.22, w: CW - 0.5, h: 1.3, fontFace: HEAD, fontSize: 13, color: th.muted, margin: 0, valign: 'top' });
  });
  footer(s, th, '', 8);
}

// ----------------------------------------------------------------- 9 · thank you (dark statement)
{
  const th = DARK, s = slide(th, 8);
  s.addText('Thank you', { x: 0.62, y: 1.2, w: 8.4, h: 1.7, fontFace: HEAD, fontSize: 88, bold: true, color: th.text, margin: 0, charSpacing: -2 });
  s.addText([{ text: 'Every monitor measures the tool. ', options: { color: th.text } }, { text: 'HAVen measures the worker.', options: { color: th.accent } }],
    { x: 0.7, y: 3.25, w: 7.4, h: 1.0, fontFace: HEAD, fontSize: 28, margin: 0, valign: 'top' });
  s.addText('Next: validate against a reference meter, then pilot with an Alberta contractor.', { x: 0.7, y: 4.5, w: 7, h: 0.5, fontFace: HEAD, fontSize: 18, color: th.muted, margin: 0 });
  s.addText('Questions?', { x: 0.7, y: 5.1, w: 6, h: 0.5, fontFace: MONO, fontSize: 16, bold: true, color: th.accent, margin: 0 });
  phone(s, th, 'screen_today-dark.png', 8.9, 0.9, 5.8);
  footer(s, th, '', 9);
}

// ----------------------------------------------------------------- 10 · Q&A index (light)
{
  const th = LIGHT, s = slide(th, 9);
  pill(s, th, 'Backup · Q&A index', 0.7, 0.55);
  title(s, th, 'If a judge asks…', { h: 0.8, size: 38 });
  const qs = [
    [11, 'Who else does this?'], [12, 'What makes you different?'], [13, 'What does it cost?'], [14, 'What would the real product look like?'],
    [15, 'Who would buy it?'], [16, 'How big is the market?'], [17, 'What’s next?'], [18, 'What’s the impact?'],
    [19, 'What would you charge?'],
  ];
  qs.forEach(([num, q], i) => {
    const col = i < 5 ? 0 : 1, row = i < 5 ? i : i - 5;
    const x = 0.7 + col * 6.05, y = 2.3 + row * 0.92;
    card(s, th, x, y, 5.85, 0.8);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: x + 0.22, y: y + 0.17, w: 0.62, h: 0.46, rectRadius: 0.14, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
    s.addText(String(num), { x: x + 0.22, y: y + 0.17, w: 0.62, h: 0.46, fontFace: MONO, fontSize: 14, bold: true, color: 'FFFFFF', align: 'center', valign: 'middle', margin: 0 });
    s.addText(q, { x: x + 1.07, y: y + 0.07, w: 4.6, h: 0.66, fontFace: HEAD, fontSize: 18, color: th.text, margin: 0, valign: 'middle' });
  });
  footer(s, th, '', 10);
}

// ----------------------------------------------------------------- 11 · competition (light)
{
  const th = LIGHT, s = slide(th, 10);
  pill(s, th, 'Backup · competition', 0.7, 0.55);
  title(s, th, 'A proven market, led by UK products', { h: 0.8, size: 36 });
  const hdr = (t) => ({ text: t, options: { bold: true, color: 'FFFFFF', fill: { color: th.accent } } });
  const row = (cells, hl) => cells.map((t, i) => ({ text: t, options: { bold: hl || i === 0, color: th.text, fill: { color: hl ? 'D5EADF' : 'FFFFFF' } } }));
  s.addTable([
    [hdr('Product'), hdr('Form'), hdr('Measures'), hdr('Traction'), hdr('Price')],
    row(['Reactec R-Link', 'Wrist watch + tool tags', 'Vibration, tool time', '~70k workers, 900 firms (2021); £5.6M revenue (2023)', 'Quote only']),
    row(['Svantek SV 103', 'Pro dosimeter', 'Palm vibration, contact force', 'Used for formal assessments', '£2.3–3.5k']),
    row(['Castle HAVi', 'Watch + tool timer', 'Trigger time only', 'UK safety supplier', '£1,150']),
    row(['HAVen', 'Forearm sleeve', 'Vibration, grip muscle effort, fatigue', 'Prototype · built for Canada', 'C$18.69 parts'], true),
  ], { x: 0.7, y: 2.1, w: 11.93, colW: [2.2, 2.4, 2.9, 3.13, 1.3], fontFace: HEAD, fontSize: 14, color: th.text,
       border: { type: 'solid', pt: 0.75, color: th.hair }, valign: 'middle', margin: 0.1, rowH: [0.5, 0.8, 0.7, 0.6, 0.8] });
  footer(s, th, 'Sources: Dealroom; Project Scotland; AcSoft; Castle shop. Our cost: per sleeve at 100 units.', 11);
}

// ----------------------------------------------------------------- 12 · our edge (dark)
{
  const th = DARK, s = slide(th, 11);
  pill(s, th, 'Backup · our edge', 0.7, 0.55);
  title(s, th, 'Others measure the tool. We measure the worker.', { h: 1.3, size: 38 });
  const pts = [
    ['Fits under any glove', 'A sleeve, not a glove, so it doesn’t change how a worker holds the tool or replace the anti-vibration gloves they already wear.'],
    ['Muscle, not just force', 'Forearm EMG shows grip effort, sustained muscle load and fatigue, which are strain risks on their own.'],
    ['Long-term view', 'Day-to-year history, years-to-risk and rule-based advice in one app.'],
    ['Built for Canada', 'No incumbent here and no numeric Alberta limit, at a fraction of UK device prices.'],
  ];
  pts.forEach(([h, b], i) => {
    const x = 0.7 + (i % 2) * 6.05, y = 2.6 + Math.floor(i / 2) * 2.0;
    card(s, th, x, y, 5.88, 1.8);
    s.addText(h, { x: x + 0.35, y: y + 0.3, w: 5.2, h: 0.42, fontFace: HEAD, fontSize: 21, bold: true, color: th.accent, margin: 0 });
    s.addText(b, { x: x + 0.35, y: y + 0.82, w: 5.2, h: 0.85, fontFace: HEAD, fontSize: 15, color: th.muted, margin: 0, valign: 'top' });
  });
  footer(s, th, '', 12);
}

// ----------------------------------------------------------------- 13 · cost (light)
{
  const th = LIGHT, s = slide(th, 12);
  pill(s, th, 'Backup · cost', 0.7, 0.55);
  title(s, th, 'Commodity parts, a fraction of the price', { h: 0.8, size: 36 });
  s.addText('Manufacturing cost per sleeve (100 units)', { x: 0.7, y: 2.1, w: 7, h: 0.34, fontFace: HEAD, fontSize: 16, bold: true, color: th.text, margin: 0 });
  const hdr = (t) => ({ text: t, options: { bold: true, color: 'FFFFFF', fill: { color: th.accent } } });
  const r = (a, b, tot) => [{ text: a, options: { color: th.text, bold: !!tot, fill: { color: tot ? 'D5EADF' : 'FFFFFF' } } }, { text: b, options: { color: th.text, bold: !!tot, align: 'right', fill: { color: tot ? 'D5EADF' : 'FFFFFF' } } }];
  s.addTable([
    [hdr('Part'), { text: 'Cost (CAD)', options: { bold: true, color: 'FFFFFF', fill: { color: th.accent }, align: 'right' } }],
    r('ESP32-C3-MINI-1 module', 'C$2.52'), r('MPU-6050 accelerometer*', 'C$1.36'), r('Muscle BioAmp (EMG)', 'C$9.21'),
    r('PCB, power and charging', 'C$4.25'), r('Compression arm sleeve', 'C$1.35'), r('Total per sleeve (100 units)', 'C$18.69', true),
  ], { x: 0.7, y: 2.6, w: 7.2, colW: [5.1, 2.1], fontFace: HEAD, fontSize: 15, border: { type: 'solid', pt: 0.75, color: th.hair }, valign: 'middle', margin: 0.1, rowH: 0.5 });
  s.addText('Compared with', { x: 8.6, y: 2.1, w: 4, h: 0.34, fontFace: HEAD, fontSize: 16, bold: true, color: th.text, margin: 0 });
  [['Castle HAVi watch system', '£1,150'], ['Svantek SV 103 dosimeter', '£2,262–3,457']].forEach(([lab, big], i) => {
    const y = 2.6 + i * 1.7;
    card(s, th, 8.6, y, 4.03, 1.5);
    s.addText(lab, { x: 8.9, y: y + 0.25, w: 3.6, h: 0.3, fontFace: HEAD, fontSize: 14, color: th.muted, margin: 0 });
    s.addText(big, { x: 8.9, y: y + 0.65, w: 3.6, h: 0.6, fontFace: MONO, fontSize: 30, bold: true, color: th.text, margin: 0 });
  });
  footer(s, th, 'Competitor prices: Castle shop; AcSoft (excl. VAT). PCB line includes assembly and fees.', 13);
}

// ----------------------------------------------------------------- 14 · real product (dark)
{
  const th = DARK, s = slide(th, undefined);
  s.addNotes(NOTES[13] + ' Annotations: the black pod on the forearm is the sealed electronics, with the accelerometer and the EMG pill inside; the red and yellow discs are the dry electrodes. Right-hand panel: the price and margin (full version on backup slide 19).');
  pill(s, th, 'Backup · the real product', 0.7, 0.55);
  title(s, th, 'Prototype to site-ready sleeve', { h: 0.8, size: 36 });
  // hero render at its true proportions (2752 x 1536), with two annotations
  const HX = 0.7, HY = 2.05, HW = 6.2, HH = HW * 1536 / 2752;
  s.addImage({ path: AS('sleeve_render1.png'), x: HX, y: HY, w: HW, h: HH });
  const podX = HX + 0.625 * HW, podTop = HY + 0.417 * HH;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: HX + 3.75, y: HY - 0.02, w: 2.95, h: 0.5, rectRadius: 0.12, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addText('Sealed pod: accelerometer + EMG pill', { x: HX + 3.85, y: HY - 0.02, w: 2.8, h: 0.5, fontFace: HEAD, fontSize: 11.5, bold: true, color: '0E2B20', margin: 0, valign: 'middle' });
  s.addShape(pres.shapes.LINE, { x: podX - 0.25, y: HY + 0.48, w: 0, h: podTop - (HY + 0.48) - 0.02, line: { color: th.accent, width: 1.5 } });
  const elX = HX + 0.38 * HW, elY = HY + 0.7 * HH;
  s.addShape(pres.shapes.LINE, { x: elX, y: elY, w: 0, h: 0.42, line: { color: th.accent, width: 1.5 } });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: elX - 1.0, y: elY + 0.42, w: 2.4, h: 0.42, rectRadius: 0.12, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addText('Dry electrodes (EMG)', { x: elX - 0.9, y: elY + 0.42, w: 2.2, h: 0.42, fontFace: HEAD, fontSize: 11.5, bold: true, color: '0E2B20', margin: 0, valign: 'middle' });
  s.addImage({ path: AS('sleeve_render2.png'), x: 0.7, y: 5.6, w: 2.4, h: 2.4 * 736 / 1429 });
  s.addImage({ path: AS('sleeve_render3.png'), x: 3.3, y: 5.6, w: 2.4, h: 2.4 * 736 / 1429 });
  // business model, in the place of the feature list (price and margin, as on the backup slide 19)
  const RX = 7.9, RW = 4.7;
  s.addText('BUSINESS MODEL', { x: RX, y: 1.75, w: RW, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.accent, margin: 0 });
  const sc = (v) => RX + (v / 40) * RW;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: RX, y: 2.4, w: RW, h: 0.3, rectRadius: 0.15, fill: { color: th.card }, line: { color: th.hair, width: 0.75 } });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: sc(25), y: 2.4, w: sc(35) - sc(25), h: 0.3, rectRadius: 0.15, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addShape(pres.shapes.RECTANGLE, { x: sc(18.69) - 0.015, y: 2.27, w: 0.03, h: 0.5, fill: { color: th.red }, line: { color: th.red, width: 0 } });
  s.addText('Parts cost C$18.69', { x: sc(18.69) - 1.2, y: 2.04, w: 2.4, h: 0.22, fontFace: MONO, fontSize: 10, bold: true, color: th.red, align: 'center', margin: 0 });
  s.addText('Price $25–35', { x: sc(30) - 0.9, y: 2.76, w: 1.8, h: 0.22, fontFace: MONO, fontSize: 10, bold: true, color: th.accent, align: 'center', margin: 0 });
  const rows = [['At $25', '25%', 'C$6.31 kept per sleeve'], ['At $30', '38%', 'C$11.31 kept per sleeve'], ['At $35', '47%', 'C$16.31 kept per sleeve']];
  rows.forEach(([lab, pct, sub], i) => {
    const y = 3.2 + i * 0.7;
    card(s, th, RX, y, RW, 0.58, i === 1 ? { rim: th.accent, rimW: 1.5 } : {});
    s.addText(lab.toUpperCase(), { x: RX + 0.22, y: y, w: 1.2, h: 0.58, fontFace: MONO, fontSize: 10.5, bold: true, color: th.accent, margin: 0, valign: 'middle' });
    s.addText(pct, { x: RX + 1.35, y: y, w: 1.0, h: 0.58, fontFace: HEAD, fontSize: 24, bold: true, color: th.text, margin: 0, valign: 'middle' });
    s.addText(sub, { x: RX + 2.3, y: y, w: 2.3, h: 0.58, fontFace: HEAD, fontSize: 12.5, color: th.muted, margin: 0, valign: 'middle' });
  });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: RX, y: 5.35, w: RW, h: 1.05, rectRadius: 0.2, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addText('We can sell it for $25 to $35 a sleeve.', { x: RX + 0.25, y: 5.42, w: RW - 0.5, h: 0.4, fontFace: HEAD, fontSize: 15, bold: true, color: 'FFFFFF', margin: 0, valign: 'middle' });
  s.addText('Gross margin 25% to 47% at 100 units. Parts and assembly only.', { x: RX + 0.25, y: 5.88, w: RW - 0.5, h: 0.45, fontFace: HEAD, fontSize: 12, color: 'FFFFFF', margin: 0, valign: 'top' });
  footer(s, th, 'Concept renders of the production design, not CAD or a built part.', 14);
}

// ----------------------------------------------------------------- 15 · who buys (light)
{
  const th = LIGHT, s = slide(th, 14);
  pill(s, th, 'Backup · who buys', 0.7, 0.55);
  title(s, th, 'More buyers than just the employer', { h: 0.8, size: 36 });
  const hdr = (t) => ({ text: t, options: { bold: true, color: 'FFFFFF', fill: { color: th.accent } } });
  const row = (cells) => cells.map((t, i) => ({ text: t, options: { bold: i === 0, color: th.text, fill: { color: 'FFFFFF' } } }));
  s.addTable([
    [hdr('Buyer'), hdr('Why they’d pay'), hdr('Evidence')],
    row(['Contractors', 'Exposure records strengthen their safety program and WCB rebates', 'WCB Alberta PIR: 10% refund first COR year, 5% after']),
    row(['Unions & workers', 'A worker-owned exposure history to back a future compensation claim', 'HAVS claims are rarely accepted in Canada']),
    row(['Tool rental firms', 'Rent the sleeve with every breaker or grinder', 'Speedy Hire offered HAV meters to customers (2009)']),
    row(['Safety associations', 'Bundle with COR training and audits', 'ACSA members earned $34M in PIR rebates (2016)']),
    row(['Big project owners', 'Require safety certification from every contractor', 'City of Calgary requires COR to prequalify']),
  ], { x: 0.7, y: 2.1, w: 11.93, colW: [2.6, 5.0, 4.33], fontFace: HEAD, fontSize: 14, border: { type: 'solid', pt: 0.75, color: th.hair }, valign: 'middle', margin: 0.1, rowH: [0.5, 0.75, 0.75, 0.75, 0.75, 0.75] });
  footer(s, th, 'Sources: WCB Alberta; ACSA; JCAA; KHL (Speedy Hire); City of Calgary', 15);
}

// ----------------------------------------------------------------- 16 · market (dark)
{
  const th = DARK, s = slide(th, 15);
  pill(s, th, 'Backup · market', 0.7, 0.55);
  title(s, th, 'Start in Alberta, then expand', { h: 0.8, size: 38 });
  const bl = [
    ['Beachhead', '266,100', 'Alberta construction workers (March 2025). The share using vibrating tools daily: [__%].', th.accent],
    ['Proof it sells', '~70,000', 'workers on Reactec’s HAVwear by 2021, mostly in the UK, where limits are law.', th.accent],
    ['Next', 'Beyond construction', 'Mining, oil and gas, manufacturing, forestry, rail: anywhere hands meet vibrating tools.', th.amber],
  ];
  bl.forEach(([lab, big, body, col], i) => {
    const x = 0.7 + i * 4.07, y = 2.3;
    card(s, th, x, y, 3.86, 3.9, i === 2 ? { rim: th.amber, rimW: 1.5 } : {});
    s.addText(lab.toUpperCase(), { x: x + 0.3, y: y + 0.3, w: 3.3, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: col, margin: 0 });
    s.addText(big, { x: x + 0.3, y: y + 0.8, w: 3.3, h: 0.9, fontFace: HEAD, fontSize: i === 2 ? 26 : 44, bold: true, color: th.text, margin: 0, valign: 'middle' });
    s.addText(body, { x: x + 0.3, y: y + 1.95, w: 3.3, h: 1.7, fontFace: HEAD, fontSize: 15, color: th.muted, margin: 0, valign: 'top' });
  });
  footer(s, th, 'Sources: Job Bank Alberta construction profile; Project Scotland (Reactec, 2021)', 16);
}

// ----------------------------------------------------------------- 17 · roadmap (light)
{
  const th = LIGHT, s = slide(th, 16);
  pill(s, th, 'Backup · where this goes', 0.7, 0.55);
  title(s, th, 'From prototype to pilot', { h: 0.8, size: 38 });
  const st = [
    ['NOW', 'Working prototype', 'Sleeve, app, ISO-based exposure, limits and trends.'],
    ['NEXT 3 MONTHS', 'Validate', 'Compare against a reference meter; talk to Edmonton unions and contractors.'],
    ['6–12 MONTHS', 'Productize', 'Custom board, dry electrodes, ISO frequency weighting, AI tool recognition.'],
    ['THEN', 'Pilot & scale', 'Paid site pilot, supervisor dashboard, then mining and manufacturing.'],
  ];
  st.forEach(([when, h, b], i) => {
    const x = 0.7 + i * 3.02, y = 2.25;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 2.85, h: 0.46, rectRadius: 0.23, fill: { color: i === 0 ? th.accent : th.card }, line: { color: th.rim, width: 2 } });
    s.addText(when, { x, y, w: 2.85, h: 0.46, fontFace: MONO, fontSize: 11, bold: true, color: i === 0 ? 'FFFFFF' : th.accent, align: 'center', valign: 'middle', margin: 0 });
    card(s, th, x, y + 0.7, 2.85, 2.9);
    s.addText(h, { x: x + 0.25, y: y + 0.98, w: 2.4, h: 0.6, fontFace: HEAD, fontSize: 17, bold: true, color: th.text, margin: 0 });
    s.addText(b, { x: x + 0.25, y: y + 1.68, w: 2.4, h: 1.7, fontFace: HEAD, fontSize: 14, color: th.muted, margin: 0, valign: 'top' });
  });
  s.addText('natHacks funding would go to: [reference meter, production parts, pilot]', { x: 0.7, y: 6.15, w: 11.9, h: 0.4, fontFace: HEAD, fontSize: 16, bold: true, color: th.text, margin: 0 });
  footer(s, th, '', 17);
}

// ----------------------------------------------------------------- 18 · impact (dark statement)
{
  const th = DARK, s = slide(th, 17);
  pill(s, th, 'Backup · impact', 0.7, 0.55);
  title(s, th, 'Measuring exposure cuts cases', { h: 0.8, size: 40 });
  const big = [
    ['1,210 → 220', 'new UK HAVS benefit cases, 2010 vs 2024, after limits became law in 2005.'],
    ['145,000', 'vibration white finger claims settled for former UK coal miners.'],
    ['Rare', 'accepted HAVS claims in Canada: workers can’t prove exposure they never recorded.'],
  ];
  big.forEach(([num, body], i) => {
    const x = 0.7 + i * 4.07;
    s.addText(num, { x, y: 2.7, w: 3.86, h: 1.1, fontFace: HEAD, fontSize: 40, bold: true, color: th.accent, margin: 0, valign: 'middle' });
    s.addText(body, { x, y: 4.0, w: 3.6, h: 1.4, fontFace: HEAD, fontSize: 17, color: th.text, margin: 0, valign: 'top', lineSpacingMultiple: 1.2 });
  });
  footer(s, th, 'Sources: HSE IIDB statistics; UK National Audit Office; JCAA', 18);
}

// ----------------------------------------------------------------- 19 · price and margin (light)
{
  const th = LIGHT, s = slide(th, undefined);
  s.addNotes('Q: “What would you charge?” Answer: “We’d price it at $25 to $35 per sleeve. At 100 units it costs about C$18.69 to make, so the gross margin is roughly 25% at $25 and 47% at $35. That covers parts and assembly only; software, support, shipping and sales come on top.” CAUTION: the price is an assumption until we test it with contractors; say so. Margin = (price − parts cost) ÷ price.');
  pill(s, th, 'Backup · price and margin', 0.7, 0.55);
  title(s, th, 'We can sell it for $25 to $35 a sleeve', { h: 0.8, size: 36 });
  // price scale: $0 to $40, with the parts cost and the price range marked
  const L = 0.7, W = 11.93, BY = 3.05, scale = (v) => L + (v / 40) * W;
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: L, y: BY, w: W, h: 0.4, rectRadius: 0.2, fill: { color: 'FFFFFF' }, line: { color: th.rim, width: 2 } });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: scale(25), y: BY, w: scale(35) - scale(25), h: 0.4, rectRadius: 0.2, fill: { color: th.accent }, line: { color: th.accent, width: 0 } });
  s.addShape(pres.shapes.RECTANGLE, { x: scale(18.69) - 0.015, y: BY - 0.25, w: 0.03, h: 0.9, fill: { color: th.red }, line: { color: th.red, width: 0 } });
  s.addText('Parts cost C$18.69', { x: scale(18.69) - 1.6, y: BY - 0.55, w: 3.2, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.red, align: 'center', margin: 0 });
  s.addText('Price $25–35', { x: scale(25), y: BY - 0.55, w: scale(35) - scale(25), h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.accent, align: 'center', margin: 0 });
  ['$0', '$10', '$20', '$30', '$40'].forEach((t, i) => s.addText(t, { x: scale(i * 10) - 0.4, y: BY + 0.5, w: 0.8, h: 0.25, fontFace: MONO, fontSize: 9.5, color: th.muted, align: 'center', margin: 0 }));
  const cards = [
    ['At $25', '25%', 'gross margin', 'C$6.31 kept per sleeve'],
    ['At $30', '38%', 'gross margin', 'C$11.31 kept per sleeve'],
    ['At $35', '47%', 'gross margin', 'C$16.31 kept per sleeve'],
  ];
  cards.forEach(([lab, big, unit, sub], i) => {
    const x = 0.7 + i * 4.07, y = 4.1;
    card(s, th, x, y, 3.86, 2.2, i === 1 ? { rim: th.accent, rimW: 2.5 } : {});
    s.addText(lab.toUpperCase(), { x: x + 0.3, y: y + 0.25, w: 3.3, h: 0.28, fontFace: MONO, fontSize: 11, bold: true, color: th.accent, margin: 0 });
    s.addText([{ text: big, options: { fontSize: 46, bold: true, color: th.text } }, { text: '  ' + unit, options: { fontSize: 15, color: th.muted } }],
      { x: x + 0.3, y: y + 0.7, w: 3.4, h: 0.85, fontFace: HEAD, margin: 0, valign: 'middle' });
    s.addText(sub, { x: x + 0.3, y: y + 1.6, w: 3.3, h: 0.35, fontFace: HEAD, fontSize: 14, color: th.muted, margin: 0 });
  });
  footer(s, th, 'Gross margin = (price − parts cost) ÷ price. Parts and assembly at 100 units; excludes software, shipping, warranty and sales. Prices assumed in the same currency as the cost (CAD).', 19);
}

pres.writeFile({ fileName: path.join(__dirname, 'HAVen_Pitch_Deck.pptx') }).then((f) => console.log('wrote', f, 'slides', n));
