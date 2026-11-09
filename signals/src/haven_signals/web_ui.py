"""vg-live --web: local browser page for calibration and live results.

Serves http://127.0.0.1:8080 (localhost only). It reads the same derived-record
stream as the text view (bridge fan-out, default 127.0.0.1:8767) and sends the
same commands (relaxed, squeeze, cancel, newshift) back to the bridge.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPTURE_S = 3.5

PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HAVen calibration</title>
<style>
body{font:16px/1.45 system-ui,sans-serif;margin:0;padding:16px;background:#f6f4ef;color:#1d1d1b;max-width:720px}
h1{font-size:1.3rem;margin:0 0 10px}
.card{background:#fff;border:1px solid #d9d5cc;border-radius:10px;padding:12px 14px;margin:10px 0}
.big{font-size:2.2rem;font-weight:700;font-variant-numeric:tabular-nums}
.lbl{color:#4a4843;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
button{font:inherit;padding:12px 14px;margin:4px 4px 4px 0;border-radius:8px;border:1px solid #1d1d1b;background:#fff;cursor:pointer;min-width:132px}
button.go{background:#1d1d1b;color:#fff}
.stage{font-size:1.2rem;font-weight:700}
.bar{height:18px;background:#e7e4dc;border-radius:9px;overflow:hidden}
.bar>div{height:100%;background:#2b6cb0;width:0;transition:width .2s}
.ok{color:#15803d;font-weight:700}.bad{color:#b91c1c;font-weight:700}
.mono{font-family:ui-monospace,monospace;font-size:13px;color:#4a4843}
@media (prefers-color-scheme: dark){body{background:#16171a;color:#ecebe6}.card{background:#1f2024;border-color:#34363c}button{background:#1f2024;color:#ecebe6;border-color:#ecebe6}button.go{background:#ecebe6;color:#16171a}.bar{background:#34363c}.lbl,.mono{color:#a3a19a}}
</style>
<h1>HAVen calibration</h1>
<div class="card">
  <div class="lbl">Step</div>
  <div class="stage" id="stage">Waiting for the wearable…</div>
  <div class="bar" style="margin-top:8px"><div id="cbar"></div></div>
  <div style="margin-top:10px">
    <button id="b_rel" onclick="cmd('relaxed')">1. Relaxed (4 s)</button>
    <button id="b_sq" onclick="cmd('squeeze')">2. Max squeeze (4 s)</button>
    <button onclick="cmd('cancel')">Cancel</button>
  </div>
  <div class="mono" id="hint">Press Relaxed, then stay completely still and relaxed.</div>
</div>
<div class="card">
  <div class="lbl">Result</div>
  <div id="result">Not calibrated yet.</div>
</div>
<div class="card">
  <div class="lbl">Grip effort (% of your max squeeze)</div>
  <div class="big" id="grip">-</div>
  <div class="bar" style="margin-top:8px"><div id="gbar"></div></div>
</div>
<div class="card">
  <div class="lbl">Signal</div>
  <div class="mono" id="sig">-</div>
</div>
<div class="card">
  <div class="lbl">Tool use</div>
  <div class="mono" id="tool">-</div>
  <div style="margin-top:8px"><button onclick="cmd('newshift')">New shift (reset tool time)</button></div>
</div>
<div class="card">
  <div class="lbl">Tool calibration: tool vibration vs normal movement</div>
  <div class="stage" id="tstage">Not calibrated (using default thresholds).</div>
  <div class="bar" style="margin-top:8px"><div id="tbar"></div></div>
  <div style="margin-top:10px">
    <button onclick="cmd('toolcal:still')">1. Still (5 s)</button>
    <button onclick="cmd('toolcal:movement')">2. Normal movement (12 s)</button>
    <button class="go" onclick="cmd('toolcal:tool')">3. Tool ON (10 s)</button>
    <button onclick="cmd('toolcal:cancel')">Cancel</button>
    <button onclick="cmd('toolcal:reset')">Reset to defaults</button>
  </div>
  <div class="mono" id="thint">1: arm resting, gun off. 2: gun OFF; shake hands, wave, swing the arm, tap the table, for the whole 12 s.
3: hold the running gun by its handle, like a drill, for the whole 10 s.</div>
  <div id="tresult" style="margin-top:8px"></div>
</div>
<div class="card">
  <div class="lbl">Recording labels (optional; press when you START each activity)</div>
  <div>
    <button onclick="lab('still')">Still</button>
    <button onclick="lab('arm-movement')">Arm movement</button>
    <button onclick="lab('gun-low')">Gun LOW</button>
    <button onclick="lab('gun-medium')">Gun MEDIUM</button>
    <button onclick="lab('gun-high')">Gun HIGH</button>
    <button onclick="lab('gun-off')">Gun OFF</button>
  </div>
  <div class="mono" id="curlabel">current label: none</div>
</div>
<div class="card">
  <div class="lbl">Recent events</div>
  <div class="mono" id="log"></div>
</div>
<script>
let capStart=null, capPhase=null;
function cmd(c){fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});
  if(c==='relaxed'||c==='squeeze'){capStart=Date.now();capPhase=c;}}
function lab(n){cmd('label:'+n);document.getElementById('curlabel').textContent='current label: '+n+' (since '+new Date().toLocaleTimeString()+')';}
function f(v,d){return v==null?'-':Number(v).toFixed(d)}
const EMGWHY={interrupted_stale:'the Wi-Fi link paused for over 1 s during the capture (not your electrodes). Just try again; keep the board antenna clear of your arm.',
  interrupted_gap:'data was lost during the capture (Wi-Fi). Just try again.', interrupted_signal_reset:'data was lost during the capture (Wi-Fi). Just try again.',
  clipped:'the signal hit the top of the input range too often. Squeeze a little less hard (80-90 %).',
  insufficient_separation:'the squeeze was not clearly stronger than rest. Squeeze harder, or check the pads.',
  insufficient_data:'not enough signal during the capture. Check the pads and try again.',
  cancelled:'cancelled.', interrupted_boot_change:'the wearable restarted. Start again from Relaxed.'};
const REASON={movement_looks_like_tool:'some normal movement looked like steady tool vibration. Repeat 2 with the gun fully off, or hold the gun more firmly in 3',
  tool_not_reliably_detected:'the tool was not clearly different from normal movement. Hold the running gun firmly by its handle for the whole capture',
  no_tool_vibration_detected:'no vibration seen during step 3. Is the gun on and the IMU on the same arm?',
  no_movement_detected:'hardly any movement during step 2. Move more (handshakes, waving)',
  insufficient_data:'not enough data, check the wearable connection', interrupted_data_gap:'data dropped during the capture, try again',
  interrupted_boot_change:'the wearable restarted, try again', movement_capture_required_first:'do step 2 before step 3'};
function toolResult(c){
  if(!c) return '';
  if(c.rejected) return '<span class="bad">'+(REASON[c.rejected]||c.rejected)+'</span>';
  if(c.state==='failed'&&!c.summaries) return '<span class="bad">Failed:</span> '+(REASON[c.reason]||c.reason);
  if(!c.summaries) return '';
  const row=(n,x)=>x?'<tr><td>'+n+'</td><td>'+f(x.tool_band_ms2.median,2)+'</td><td>'+f(x.motion_band_ms2.median,2)+'</td><td>'+f(x.ratio.median,2)+'</td><td>'+f(x.concentration.median,2)+'</td></tr>':'';
  const S=c.summaries, E=c.evaluation||{}, T=c.thresholds||{};
  let h=(c.state==='calibrated'?'<span class="ok">Calibrated</span>':'<span class="bad">Failed: '+(REASON[c.reason]||c.reason)+'</span>');
  h+='<table class="mono" style="margin-top:6px;border-collapse:collapse"><tr><td>medians</td><td>tool band m/s²</td><td>arm m/s²</td><td>tool/arm</td><td>periodicity</td></tr>'+row('still',S.still)+row('movement',S.movement)+row('tool',S.tool)+'</table>';
  h+='<div class="mono">tool windows accepted '+f(100*(E.tool_windows_accepted||0),0)+' % · movement windows accepted '+f(100*(E.movement_windows_accepted||0),0)+' % · false tool bouts during movement '+(E.movement_false_bouts??'-')+'</div>';
  h+='<div class="mono">'+(c.state==='calibrated'?'new':'proposed (not applied)')+' thresholds: tool band ≥ '+f(T.on_threshold_ms2,2)+' m/s², tool/arm ≥ '+f(T.tool_to_motion_min,2)+', periodicity ≥ '+f(T.concentration_min,2)+', steady for 2 s</div>';
  return h;}
async function tick(){
  try{
    const s=await (await fetch('/state')).json();
    const cal=s.calibration||{}, st=cal.state||'uncalibrated';
    let msg='', pct=0;
    if(st.startsWith('capturing')){
      const phase=st.includes('relaxed')?'relaxed':'squeeze';
      const left=Math.max(0,(cal.capture_left_s??(capStart?3.5-(Date.now()-capStart)/1000:3.5)));
      msg=(phase==='relaxed'?'STAY RELAXED':'SQUEEZE AT MAXIMUM')+' — '+left.toFixed(1)+' s left';
      pct=100*(1-left/3.5);
    } else if(st==='relaxed_captured'){msg='Relaxed recorded. Now press Max squeeze and hold.';}
    else if(st==='calibrated'){msg='Calibrated. Grip effort below is live.';}
    else if(st==='failed'||st==='invalidated'){msg='Calibration failed: '+(EMGWHY[cal.reason]||((cal.reason||st)+'. Check the pads and try again.'));}
    else {msg='Not calibrated. Press Relaxed to start.';}
    document.getElementById('stage').textContent=msg;
    document.getElementById('cbar').style.width=pct+'%';
    const res=document.getElementById('result');
    if(st==='calibrated'){res.innerHTML='<span class="ok">Calibrated</span><div class="mono">relaxed '+f(cal.relaxed,1)+' counts · max '+f(cal.max,1)+' counts · ratio '+f((cal.max||0)/Math.max(cal.relaxed||1e-9,1e-9),1)+'×</div>';}
    else if(st==='failed'){res.innerHTML='<span class="bad">Failed</span> '+(cal.reason||'')}
    const g=s.grip;
    document.getElementById('grip').textContent=g==null?'-':(100*g).toFixed(0)+' %';
    document.getElementById('gbar').style.width=g==null?'0%':Math.max(0,Math.min(100,100*g))+'%';
    document.getElementById('sig').textContent='EMG envelope '+f(s.emg_env,0)+' counts · status '+(s.emg_status||'-')+(s.emg_reasons?' ('+s.emg_reasons+')':'')+' · clipped '+(s.clipped?'YES':'no');
    const t=s.tool||{};
    document.getElementById('tool').textContent=(t.state||'-')+' · tool band '+f(t.tool,2)+' m/s² @ '+f(t.hz,0)+' Hz · arm '+f(t.arm,2)+' m/s² · periodicity '+f(t.conc,2)+' · steadiness '+f(t.steady,2)+' · tool time '+f(t.tool_time_s,1)+' s · bouts '+(t.bouts??'-')+' · thresholds '+(t.source||'-');
    const tc=t.cal||{}, ts=tc.state||'uncalibrated', TXT={still:'KEEP STILL, gun off',movement:'MOVE NORMALLY: handshakes, waving, swinging, tapping (gun OFF)',tool:'GUN ON: hold it by the handle like a drill'};
    let tm='', tp=0;
    if(ts.startsWith('capturing')){const ph=ts.slice(10), L=tc.capture_left_s, D=(s.toolcal&&s.toolcal.duration_s)||10; tm=TXT[ph]+' — '+f(L,1)+' s left'; tp=L==null?0:100*(1-L/D);}
    else if(ts==='still_captured') tm='Still recorded. Now press 2 (normal movement).';
    else if(ts==='movement_captured') tm='Movement recorded. Now switch the gun on, then press 3.';
    else if(ts==='calibrated') tm='Calibrated for this wearer, mounting and tool. Test it: shake hands (should stay no_tool), then use the gun (tool after ~2 s).';
    else if(ts==='failed') tm='Tool calibration failed: '+(s.toolcal&&s.toolcal.reason||'')+'. Defaults kept.';
    else if(ts==='cancelled') tm='Cancelled.';
    else tm='Not calibrated (using default thresholds). Start with 1.';
    document.getElementById('tstage').textContent=tm;
    document.getElementById('tbar').style.width=tp+'%';
    document.getElementById('tresult').innerHTML=toolResult(s.toolcal);
    document.getElementById('log').innerHTML=(s.events||[]).slice(-8).map(e=>e).join('<br>');
  }catch(e){document.getElementById('stage').textContent='No connection to vg-live. Is the bridge running?';}
}
setInterval(tick,250); tick();
</script>
"""


class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"calibration": {}, "events": [], "connected": False}


def start_web(shared: Shared, bridge_host: str, bridge_port: int, web_port: int, send_cmd) -> None:
    """Background threads: read the bridge stream into shared state; serve the page."""

    def reader():
        while True:
            try:
                sock = socket.create_connection((bridge_host, bridge_port), timeout=5)
                sock.settimeout(None)
                with shared.lock:
                    shared.state["connected"] = True
                buf = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        try:
                            absorb(shared, json.loads(line))
                        except (ValueError, KeyError):
                            pass
                sock.close()
            except OSError:
                pass
            with shared.lock:
                shared.state["connected"] = False
            time.sleep(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/state"):
                with shared.lock:
                    body = json.dumps(shared.state).encode()
                self._send(200, "application/json", body)
            else:
                self._send(200, "text/html; charset=utf-8", PAGE.encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                c = json.loads(self.rfile.read(n) or b"{}").get("cmd", "")
            except ValueError:
                c = ""
            if (c in ("relaxed", "squeeze", "cancel", "newshift") or c.startswith("label:")
                    or c in ("toolcal:still", "toolcal:movement", "toolcal:tool",
                             "toolcal:cancel", "toolcal:reset")):
                send_cmd(c)
                self._send(200, "application/json", b'{"ok":true}')
            else:
                self._send(400, "application/json", b'{"ok":false}')

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    threading.Thread(target=reader, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def absorb(shared: Shared, o: dict) -> None:
    """Update the page state from one derived record (same fields as the text view)."""
    t = o["type"]
    with shared.lock:
        s = shared.state
        if t == "stream_features" and o["stream"] == "emg":
            e = o["features"]["emg_activation"]
            env = e.get("envelope") or {}
            s["emg_env"] = (env.get("value") or {}).get("emg0")
            s["emg_status"] = env.get("status")
            s["emg_reasons"] = ",".join(env.get("reasons") or [])
            s["clipped"] = "clipped" in (env.get("reasons") or [])
            norm = (e.get("normalized") or {}).get("value") or {}
            s["grip"] = norm.get("emg0") if (e.get("normalized") or {}).get("status") in ("ok", "degraded") else None
            cal = e.get("calibration") or {}
            s["calibration"] = {**s.get("calibration", {}), "state": cal.get("state")}
            st = str(cal.get("state") or "")
            if st.startswith("capturing") and s.get("emg_env") is not None:
                key = "cap_relaxed" if "relaxed" in st else "cap_squeeze"
                s.setdefault(key, []).append(s["emg_env"])
        elif t == "stream_event" and o["event"] == "calibration_state":
            d = o.get("detail") or {}
            if d.get("state") == "relaxed_captured":
                vals = sorted(s.get("cap_relaxed", []))
                s["relaxed"] = vals[len(vals) // 2] if vals else None
                s["cap_relaxed"] = []
            if d.get("state") == "calibrated":
                vals = sorted(s.get("cap_squeeze", []))
                s["max"] = vals[len(vals) // 2] if vals else None
                s["cap_squeeze"] = []
                s["calibration"]["relaxed"] = s.get("relaxed")
                s["calibration"]["max"] = s.get("max")
            if d.get("state") in ("failed", "invalidated", "uncalibrated"):
                s["cap_relaxed"], s["cap_squeeze"] = [], []
            s["calibration"] = {**s.get("calibration", {}), "state": d.get("state"), "reason": d.get("reason")}
            s.setdefault("events", []).append(f"{time.strftime('%H:%M:%S')} calibration → {d.get('state')}")
        elif t == "tool_use":
            s["tool"] = {"state": o["state"], "tool": o.get("tool_band_rms_ms2"),
                         "arm": o.get("motion_band_rms_ms2"), "tool_time_s": o.get("session_tool_s"),
                         "bouts": o.get("bouts"), "hz": o.get("dominant_hz"),
                         "conc": o.get("spectral_concentration"), "steady": o.get("steadiness"),
                         "cal": o.get("calibration"), "source": (o.get("thresholds") or {}).get("source")}
        elif t == "stream_event" and o["event"] == "tool_calibration_state":
            d = o.get("detail") or {}
            prev = s.get("toolcal") or {}
            if str(d.get("state", "")).startswith("capturing") and d.get("phase") == "still":
                prev = {}
            s["toolcal"] = {**{k: prev[k] for k in ("duration_s",) if k in prev}, **d}
            s.setdefault("events", []).append(f"{time.strftime('%H:%M:%S')} tool calibration → {d.get('state')}"
                                              + (f" ({d['reason']})" if d.get("reason") else ""))
        elif t == "stream_event" and o["event"] == "tool_calibration_rejected":
            s["toolcal"] = {**(s.get("toolcal") or {}), "rejected": (o.get("detail") or {}).get("reason")}
        if t == "stream_event" and o["event"] in ("boot_change", "stream_stale", "stream_recovered"):
            s.setdefault("events", []).append(f"{time.strftime('%H:%M:%S')} {o['event']} {o.get('stream') or ''}")
        s["events"] = s.get("events", [])[-20:]
