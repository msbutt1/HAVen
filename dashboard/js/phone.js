/*
 * Share this phone's position with the backend so the worker shows up on the dashboard map.
 * Sharing only starts when the person ticks the consent box and presses Start; Stop removes the position at once.
 * The browser asks for location permission itself (it needs https, or localhost).
 */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SEND_EVERY_MS = 10000;
  let watchId = null;
  let lastSent = 0;
  let wid = null;

  const store = {
    get(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const params = new URLSearchParams(location.search);
  $('ph-id').value = params.get('worker') || store.get('vg-worker-id');
  $('ph-name').value = params.get('name') || store.get('vg-worker-name');
  $('ph-sleeve').value = params.get('sleeve') || store.get('vg-worker-sleeve');

  const say = (t) => { $('ph-status').textContent = t; };
  const api = (method, path, body) => fetch(path, {
    method, headers: body ? { 'content-type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined,
  });

  $('ph-ok').addEventListener('change', () => { $('ph-start').disabled = !$('ph-ok').checked || watchId !== null; });

  $('ph-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!$('ph-ok').checked || watchId !== null) return;
    if (!('geolocation' in navigator)) { say('This browser cannot share a location.'); return; }
    wid = $('ph-id').value.trim();
    store.set('vg-worker-id', wid);
    store.set('vg-worker-name', $('ph-name').value.trim());
    store.set('vg-worker-sleeve', $('ph-sleeve').value.trim());
    const body = {};
    if ($('ph-name').value.trim()) body.label = $('ph-name').value.trim();
    if ($('ph-sleeve').value.trim()) body.runtime = $('ph-sleeve').value.trim();
    try {
      const r = await api('PUT', `/api/workers/${encodeURIComponent(wid)}`, body);
      if (!r.ok) { say(`The server refused this worker: ${(await r.json().catch(() => ({}))).detail || r.status}`); return; }
    } catch (err) { say('Cannot reach the server.'); return; }
    say('Waiting for your phone to find its position…');
    lastSent = 0;
    watchId = navigator.geolocation.watchPosition(onPosition, onError, { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
    $('ph-start').hidden = true; $('ph-stop').hidden = false;
    for (const id of ['ph-id', 'ph-name', 'ph-sleeve', 'ph-ok']) $(id).disabled = true;
  });

  async function onPosition(pos) {
    const now = Date.now();
    if (now - lastSent < SEND_EVERY_MS) return;
    lastSent = now;
    const c = pos.coords;
    try {
      const r = await api('POST', `/api/workers/${encodeURIComponent(wid)}/position`, {
        lat: c.latitude, lon: c.longitude, accuracy_m: Number.isFinite(c.accuracy) ? c.accuracy : undefined,
        at: new Date(pos.timestamp).toISOString(), source: 'live',
      });
      say(r.ok ? `Sharing. Last sent ${new Date().toLocaleTimeString()}, accurate to about ${Math.round(c.accuracy || 0)} m.`
               : `The server did not accept the position (${r.status}).`);
    } catch (err) { say('Cannot reach the server. Will keep trying.'); }
  }

  function onError(err) {
    say(err.code === 1 ? 'Location permission was refused, so nothing is shared. Allow it in the browser settings and start again.'
      : err.code === 2 ? 'The phone cannot find its position right now.' : 'Finding your position took too long. Still trying.');
    if (err.code === 1) stop(false);
  }

  async function stop(removeMsg = true) {
    if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    watchId = null;
    try { if (wid) await api('DELETE', `/api/workers/${encodeURIComponent(wid)}/position`); } catch (e) { /* offline */ }
    $('ph-start').hidden = false; $('ph-stop').hidden = true;
    for (const id of ['ph-id', 'ph-name', 'ph-sleeve', 'ph-ok']) $(id).disabled = false;
    $('ph-start').disabled = !$('ph-ok').checked;
    if (removeMsg) say('Stopped. Your position was removed from the map.');
  }
  $('ph-stop').addEventListener('click', () => stop(true));
})();
