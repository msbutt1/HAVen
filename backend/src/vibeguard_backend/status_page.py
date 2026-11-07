"""A small status page at / so opening the server in a browser explains itself.

This is a developer/operator view of the backend (device state, provenance, latest values,
sessions), not the dashboard. It polls the same JSON API the dashboard uses.
"""

STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VibeGuard backend</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--mut:#57534e;--card:#fff;--line:#e7e5e4;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--sim:#6d28d9;--rep:#1d4ed8;--live:#15803d}
@media(prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--mut:#a8a29e;--card:#292524;--line:#44403c;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--sim:#c4b5fd;--rep:#93c5fd;--live:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:26px 0 8px}
p.sub{margin:0;color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.row{display:flex;flex-wrap:wrap;gap:8px 28px}.k{color:var(--mut);font-size:12px}.v{font-size:18px;font-weight:600}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;border:1.5px solid currentColor;font-weight:700;font-size:13px}
.simulated{color:var(--sim)}.replay{color:var(--rep)}.live{color:var(--live)}.ready,.recording,.completed{color:var(--ok)}.calibrating{color:var(--warn)}.disconnected,.error{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}th{color:var(--mut);font-weight:500;font-size:12px}
a{color:inherit}code{font:13px ui-monospace,monospace;background:var(--line);padding:1px 5px;border-radius:4px}
.note{color:var(--mut);font-size:13px;margin-top:6px}#err{color:var(--bad);display:none;margin-top:10px}
</style></head><body><main>
<h1>VibeGuard backend</h1>
<p class="sub">Local server for device data, sessions, replay and history. This page is a status view; the dashboard is a separate app.</p>
<div id="err">Cannot reach the API.</div>

<h2>Wearable</h2>
<div class="card"><div class="row">
 <div><div class="k">State</div><div class="v" id="state">-</div></div>
 <div><div class="k">Data source</div><div class="v" id="prov">-</div></div>
 <div><div class="k">Last data</div><div class="v" id="age">-</div></div>
 <div><div class="k">Calibration</div><div class="v" id="cal">-</div></div>
 <div><div class="k">Recording</div><div class="v" id="rec">-</div></div>
</div><div class="note" id="provnote"></div><div class="note" id="calnote"></div></div>

<h2>Latest measurements</h2>
<div class="card"><div class="row">
 <div><div class="k">Vibration (unweighted, measured)</div><div class="v" id="vib">-</div></div>
 <div><div class="k">Muscle activation (relative proxy, 0-1)</div><div class="v" id="act">-</div></div>
 <div><div class="k">Experimental score (arbitrary units)</div><div class="v" id="score">-</div></div>
</div><div class="note">Three separate quantities. The score is an experimental formula, not a vibration dose. A dash means unavailable, never zero.</div></div>

<h2>Counters</h2>
<div class="card"><table><tbody id="counters"></tbody></table></div>

<h2>Recent sessions</h2>
<div class="card"><table><thead><tr><th>Started</th><th>Label</th><th>State</th><th>Source</th><th>Score</th></tr></thead><tbody id="sessions"></tbody></table></div>

<h2>API</h2>
<div class="card"><a href="/docs">Interactive API docs</a> &middot; <a href="/api/health"><code>/api/health</code></a> &middot;
<a href="/api/runtimes"><code>/api/runtimes</code></a> &middot; <a href="/api/history"><code>/api/history</code></a> &middot;
<code>ws://&lt;this host&gt;/ws/live</code></div>
</main>
<script>
const $=id=>document.getElementById(id);
const badge=(t,c)=>'<span class="badge '+(c||t)+'">'+t.toUpperCase()+'</span>';
const CAL={uncalibrated:'Not calibrated yet. Capture a relaxed and then a squeeze reading (POST /api/runtimes/wearable/calibration).',
 capturing_relaxed:'Capturing the relaxed reading (3 s of device time). Keep the muscle relaxed.',
 relaxed_captured:'Relaxed reading done. Now capture the squeeze reading.',
 capturing_squeeze:'Capturing the squeeze reading (3 s of device time). Squeeze.',
 calibrated:'Calibrated: activation is shown relative to this wearer\u2019s relaxed and squeeze readings.',
 failed:'The capture failed (for example the squeeze was not clearly stronger than relaxed). Try again.',
 provenance_mismatch:'A saved calibration came from a different recording or data source, so it was not applied. Calibrate again.',
 boot_mismatch:'The device restarted, so the old calibration was dropped. Calibrate again.',
 boot_change:'The device restarted, so the old calibration was dropped. Calibrate again.',
 device_mismatch:'A saved calibration belongs to a different device, so it was not applied. Calibrate again.'};
const num=(v,d)=>v==null?'-':Number(v).toFixed(d);
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()}
async function tick(){
 try{
  const [rt,ss]=await Promise.all([j('/api/runtimes/wearable').catch(()=>null),j('/api/sessions?limit=8')]);
  $('err').style.display='none';
  if(rt){
   $('state').innerHTML=badge(rt.state);
   const p=rt.provenance;$('prov').innerHTML=p?badge(p.source):'-';
   $('provnote').textContent=!p?'':p.source==='simulated'?'SIMULATED device: synthetic data, not a measurement of anyone.':p.source==='replay'?'REPLAY of an earlier recording (original: '+(p.original_source||'?')+'), not live.':'Live device data.';
   $('age').textContent=rt.last_rx_age_s==null?'none yet':rt.last_rx_age_s.toFixed(2)+' s ago';
   $('cal').textContent=rt.calibration.state+(rt.calibration.reason?' ('+rt.calibration.reason+')':'');
   $('calnote').textContent=CAL[rt.calibration.reason]||CAL[rt.calibration.state]||'';
   $('rec').textContent=rt.active_session_id?'session '+rt.active_session_id.slice(-6):'not recording';
   const L=rt.latest||{},imu=L.imu&&L.imu.features&&L.imu.features.vibration,emg=L.emg&&L.emg.features&&L.emg.features.emg_activation,sc=L.score;
   const v=imu&&imu.physical&&imu.physical.status==='ok'?imu.physical.combined_rms:null;
   $('vib').textContent=v==null?'-':num(v,2)+' m/s\\u00b2';
   const a=emg&&emg.normalized&&emg.normalized.status==='ok'?emg.normalized.display.emg0:null;
   $('act').textContent=a==null?'-':num(a,2);
   $('score').textContent=sc&&sc.status==='accumulating'?num(sc.cumulative.value,4)+' au (accumulating)':sc?num(sc.cumulative.value,4)+' au (paused: '+(sc.reasons[0]||'')+')':'-';
   $('counters').innerHTML=[...Object.entries(rt.counters),...Object.entries(rt.events)].map(([k,x])=>'<tr><td>'+k+'</td><td>'+x+'</td></tr>').join('')||'<tr><td colspan=2>nothing yet</td></tr>';
  }else{$('state').innerHTML=badge('no device','disconnected');$('prov').textContent='-'}
  $('sessions').innerHTML=ss.map(s=>'<tr><td>'+s.started_at.replace('T',' ').slice(0,19)+'</td><td>'+(s.label||'')+'</td><td>'+s.state+'</td><td>'+(s.source?badge(s.source):'-')+'</td><td>'+(s.score?(s.score.status==='ok'?num(s.score.session_au,4)+' au':s.score.status):'-')+'</td></tr>').join('')||'<tr><td colspan=5>none yet</td></tr>';
 }catch(e){$('err').style.display='block'}
}
tick();setInterval(tick,1000);
</script></body></html>
"""
