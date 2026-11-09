package com.vibeguard.app;

import android.content.Context;
import android.content.SharedPreferences;

import com.vibeguard.app.engine.Emg;
import com.vibeguard.app.engine.Event;
import com.vibeguard.app.engine.Exposure;
import com.vibeguard.app.engine.Processor;
import com.vibeguard.app.engine.ToolUse;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * The whole standalone app (the Android counterpart of the iOS AppModel): the link to the sleeve, the on-phone
 * processor (com.vibeguard.app.engine), the grip and tool calibrations, the exposure score and tool time for the
 * shift. Everything runs on the phone.
 *
 * The sleeve joins this phone's hotspot and, when no laptop bridge answers its discovery broadcast, connects to
 * the hotspot gateway (this phone) on TCP port 8765. All engine work happens on one thread ({@link #ex}); the
 * screen reads {@link #stateJson()}.
 */
final class Sleeve {
    static final int PORT = 8765;
    private static Sleeve instance;

    static synchronized Sleeve get(Context c) {
        if (instance == null) instance = new Sleeve(c.getApplicationContext());
        return instance;
    }

    private final SharedPreferences prefs;
    private final ScheduledExecutorService ex = Executors.newSingleThreadScheduledExecutor();
    private Processor proc;
    private String source;                       // "sleeve" | "demo"
    private double countsPerG, personalLimit;
    private boolean running;
    private volatile String state = "{}";

    // link
    private Thread serverThread;
    private ServerSocket server;
    private volatile Socket current;
    private String linkDetail = "", linkError;
    private DemoSleeve demo;
    private double lastLineAt = -1;
    private int lineCount;
    private double rateWindowStart, linesPerS;
    private boolean connected;

    // flows
    private String gripFlow, gripMessage;        // null | relaxed | squeeze; message after the last attempt
    private boolean gripDone;
    private String gripResult;                   // relaxed_done | calibrated | failed (the last finished step)
    private String toolPhase, toolMessage;
    private final Set<String> toolDone = new HashSet<>();
    private boolean toolFailed;
    // the day's log (exposure.py: counts only while a tool is in use and you are not known to be relaxed);
    // kept as the energy sum(a^2 dt) so it survives an app restart; rolls over at midnight
    private String day;
    private double dayEnergy, dayContact, dayTool;        // today's totals (demo-scaled when demo speed > 1)
    private Double lastE, lastC, lastTool;                 // the engine's totals at the previous tick
    private double lastSaved;
    // DEMO: a massage gun for a 2-minute demo. The measured vibration and the formula are unchanged; only the
    // clock runs faster: each real second of tool use counts as `demoSpeed` seconds (60 = a minute counts as
    // an hour). Shown on the Today screen while it is on.
    private double demoSpeed;
    private final ArrayDeque<String> log = new ArrayDeque<>();
    private final Set<String> shownWarnings = new HashSet<>();
    private String toast;
    private long toastId;

    private Sleeve(Context c) {
        prefs = c.getSharedPreferences("vibeguard", Context.MODE_PRIVATE);
        source = prefs.getString("source", "sleeve");
        countsPerG = prefs.getFloat("countsPerG", 4096);
        personalLimit = prefs.getFloat("personalLimit", 400);
        demoSpeed = prefs.getFloat("demoSpeed", 60);
        proc = newProcessor();
        loadDay();
        ex.scheduleAtFixedRate(this::tick, 100, 50, TimeUnit.MILLISECONDS);   // the page reads the state 10x a second
    }

    // ---- daily log ----

    private static String today() {
        return new java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(new Date());
    }

    private void loadDay() {
        day = today();
        try {
            JSONObject t = new JSONObject(prefs.getString("today", "{}"));
            if (day.equals(t.optString("date"))) {
                dayEnergy = t.optDouble("energy", 0);
                dayContact = t.optDouble("contact", 0);
                dayTool = t.optDouble("tool", 0);
            } else if (t.has("date")) {
                archive(t.optString("date"), t.optDouble("energy", 0), t.optDouble("tool", 0));
            }
        } catch (Exception ignored) {}
    }

    private double toolToday() { return dayTool; }
    private double energyToday() { return dayEnergy; }
    private double contactToday() { return dayContact; }

    private void saveDay() {
        try {
            prefs.edit().putString("today", new JSONObject().put("date", day).put("energy", energyToday())
                    .put("contact", contactToday()).put("tool", toolToday()).toString()).apply();
        } catch (Exception ignored) {}
    }

    private void archive(String date, double energy, double tool) {
        try {
            JSONObject days = new JSONObject(prefs.getString("days", "{}"));
            days.put(date, new JSONObject().put("points", Exposure.pointsFromEnergy(energy))
                    .put("a8", Exposure.a8FromEnergy(energy, 8 * 3600)).put("tool", tool));
            // keep the last 30 days
            java.util.List<String> keys = new java.util.ArrayList<>();
            for (java.util.Iterator<String> it = days.keys(); it.hasNext(); ) keys.add(it.next());
            java.util.Collections.sort(keys);
            while (keys.size() > 30) days.remove(keys.remove(0));
            prefs.edit().putString("days", days.toString()).apply();
        } catch (Exception ignored) {}
    }

    /** Called every tick: a new date archives today and starts a new day. */
    private void logDay(double t) {
        String now = today();
        if (!now.equals(day)) {
            archive(day, energyToday(), toolToday());
            day = now;
            dayEnergy = 0; dayContact = 0; dayTool = 0;
            shownWarnings.clear();
            note("New day started");
        }
        // add what the engine added since the last tick, at the demo speed
        Exposure.Snapshot x = proc.snapshot.exposure;
        double e = x.energy, c = x.contactS, tt = proc.snapshot.tool.sessionToolS;
        if (lastE != null) {
            dayEnergy += Math.max(0, e - lastE) * demoSpeed;
            dayContact += Math.max(0, c - lastC) * demoSpeed;
            dayTool += Math.max(0, tt - lastTool) * demoSpeed;
        }
        lastE = e; lastC = c; lastTool = tt;
        if (t - lastSaved > 10) { saveDay(); lastSaved = t; }
    }

    private Processor newProcessor() {
        Processor p = new Processor(countsPerG);
        p.setPersonalLimit(personalLimit);
        String saved = prefs.getString("toolCalibration", null);
        if (saved != null) {
            try {
                p.installTool(ToolUse.CalResult.fromJson(new JSONObject(saved)));
                p.takeEvents();
                toolDone.add("still"); toolDone.add("movement"); toolDone.add("tool");
            } catch (Exception ignored) {
            }
        }
        return p;
    }

    private static double now() { return System.nanoTime() / 1e9; }

    String stateJson() { return state; }

    void start() { ex.execute(this::startOnEngine); }
    void stop() { ex.execute(this::stopOnEngine); }

    private void startOnEngine() {
        if (running) return;
        running = true;
        linkError = null;
        if (source.equals("demo")) {
            demo = new DemoSleeve(line -> ex.execute(() -> ingest(line)));
            demo.start();
            linkDetail = "Simulated sleeve running in the app.";
        } else {
            linkDetail = "Waiting for the sleeve on port " + PORT + ". Turn on the hotspot and power the sleeve.";
            serverThread = new Thread(this::serve, "vg-sleeve-link");
            serverThread.start();
        }
    }

    private void stopOnEngine() {
        running = false;
        connected = false;
        if (demo != null) { demo.stop(); demo = null; }
        try { if (server != null) server.close(); } catch (Exception ignored) {}
        try { if (current != null) current.close(); } catch (Exception ignored) {}
        server = null;
        linkDetail = "";
    }

    /** TCP listener: one sleeve at a time, the newest connection wins (the sleeve reconnects after a Wi-Fi drop). */
    private void serve() {
        try (ServerSocket ss = new ServerSocket()) {
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress(PORT));
            server = ss;
            while (running) {
                Socket s = ss.accept();
                s.setTcpNoDelay(true);
                Socket old = current;
                current = s;
                if (old != null) try { old.close(); } catch (Exception ignored) {}
                String who = s.getInetAddress().getHostAddress();
                ex.execute(() -> { linkDetail = "Sleeve connected from " + who + "."; note("Sleeve connected from " + who); });
                Thread reader = new Thread(() -> read(s), "vg-sleeve-reader");
                reader.start();
            }
        } catch (Exception e) {
            if (running) {
                String why = e.getMessage();
                ex.execute(() -> { linkError = "Could not listen on port " + PORT + ": " + why; running = false; });
            }
        }
    }

    private void read(Socket s) {
        try (BufferedReader r = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8), 65536)) {
            String line;
            while ((line = r.readLine()) != null) {
                if (s != current) break;
                if (line.isEmpty() || line.length() > 256 * 1024) continue;
                final String l = line;
                ex.execute(() -> ingest(l));
            }
        } catch (Exception ignored) {
        } finally {
            try { s.close(); } catch (Exception ignored) {}
        }
    }

    // ---- engine thread ----

    private void ingest(String line) {
        if (!running) return;
        double t = now();
        if (!proc.feedLine(line, t)) return;
        lastLineAt = t;
        lineCount++;
        if (!connected) { connected = true; note("Sleeve data arriving"); }
    }

    private void tick() {
        try {
            double t = now();
            proc.poll(t);
            for (Event e : proc.takeEvents()) handle(e);
            if (t - rateWindowStart >= 1) { linesPerS = lineCount / (t - rateWindowStart); lineCount = 0; rateWindowStart = t; }
            if (connected && t - lastLineAt > 3) { connected = false; note("Sleeve stopped sending"); }
            flows();
            logDay(t);
            Processor.Snapshot s = proc.snapshot;
            for (String w : warnings(s)) if (shownWarnings.add(w)) showToast(warningText(w));
            state = build(s, t).toString();
        } catch (Exception e) {
            state = "{\"error\":" + JSONObject.quote(String.valueOf(e)) + "}";
        }
    }

    private void flows() {
        Processor.Snapshot s = proc.snapshot;
        // relax and squeeze are separate steps: each one waits for its own command
        if (gripFlow != null && !s.calibrationState.startsWith("capturing")) {
            if (gripFlow.equals("relaxed") && s.calibrationState.equals("relaxed_captured")) {
                gripResult = "relaxed_done"; gripMessage = null;
            } else if (gripFlow.equals("squeeze") && s.calibrationState.equals("calibrated")) {
                gripResult = "calibrated"; gripDone = true; gripMessage = null;
            } else {
                gripResult = "failed"; gripDone = false;
                gripMessage = gripFlow.equals("relaxed") && "clipped".equals(s.calibrationReason)
                        ? "The muscle signal is maxing out even at rest. Check that all three pads are stuck to your skin and the cables are clipped on."
                        : reasonText(s.calibrationReason, "Calibration did not complete. Try again.");
            }
            gripFlow = null;
        }
        if (toolPhase != null && !proc.toolCalState().startsWith("capturing")) {
            String st = proc.toolCalState();
            if (st.equals(toolPhase + "_captured") || st.equals("calibrated")) {
                toolDone.add(toolPhase);
                toolFailed = false;
                if (st.equals("calibrated")) showToast("Tool calibration saved");
            } else {
                toolFailed = true;
                toolMessage = st.equals("cancelled") ? "Cancelled." : reasonText(proc.toolLastFailure(), "Tool calibration did not complete. Try again.");
            }
            toolPhase = null;
        }
    }

    private void handle(Event e) {
        switch (e.kind) {
            case BOOT_CHANGE:
                if (e.state != null) { note("The sleeve restarted. Grip calibration must be redone."); prefs.edit().remove("gripCalibration").apply(); }
                else restoreGrip();
                break;
            case SEQ_GAP: if (e.missing >= 5) note(e.missing + " records lost (Wi-Fi)"); break;
            case STREAM_STALE: note(e.stream.toUpperCase() + " data paused"); break;
            case STREAM_RECOVERED: note(e.stream.toUpperCase() + " data resumed"); break;
            case SENSOR_STATE: note(e.stream.toUpperCase() + " sensor " + e.state); break;
            case CALIBRATION:
                note("Grip calibration: " + e.state.replace('_', ' ') + (e.reason != null ? " (" + e.reason + ")" : ""));
                if ("calibrated".equals(e.state) && !"restored".equals(e.reason)) saveGrip();
                break;
            case TOOL_CALIBRATION:
                note("Tool calibration: " + e.state.replace('_', ' ') + (e.reason != null ? " (" + e.reason + ")" : ""));
                if ("calibrated".equals(e.state) && !"restored".equals(e.reason) && e.result != null) {
                    try { prefs.edit().putString("toolCalibration", e.result.toJson().toString()).apply(); } catch (Exception ignored) {}
                }
                if ("uncalibrated".equals(e.state)) prefs.edit().remove("toolCalibration").apply();
                break;
            case CALIBRATION_REJECTED:
            case TOOL_CALIBRATION_REJECTED:
                showToast(reasonText(e.reason, e.reason));
                break;
            default:
        }
    }

    private void saveGrip() {
        Emg.GripCalibration g = proc.gripCalibrationActive();
        if (g == null) return;
        try {
            prefs.edit().putString("gripCalibration", new JSONObject().put("device", proc.snapshot.deviceId)
                    .put("boot", g.bootId).put("relaxed", g.relaxed).put("squeeze", g.squeeze).put("at", g.createdAtMs).toString()).apply();
        } catch (Exception ignored) {}
    }

    /** Same sleeve and same boot only: after a reboot the pads may have moved. */
    private void restoreGrip() {
        try {
            String saved = prefs.getString("gripCalibration", null);
            if (saved == null) return;
            JSONObject o = new JSONObject(saved);
            if (!o.optString("device").equals(proc.snapshot.deviceId) || !o.optString("boot").equals(proc.snapshot.bootId)) return;
            Emg.GripCalibration g = new Emg.GripCalibration();
            g.deviceId = o.optString("device"); g.bootId = o.optString("boot");
            g.relaxed = o.getDouble("relaxed"); g.squeeze = o.getDouble("squeeze"); g.createdAtMs = o.optLong("at");
            proc.installGrip(g);
        } catch (Exception ignored) {}
    }

    private void note(String s) {
        log.addLast(DateFormat.getTimeInstance(DateFormat.MEDIUM).format(new Date()) + "  " + s);
        while (log.size() > 30) log.removeFirst();
    }

    private void showToast(String s) { toast = s; toastId++; }

    // ---- commands from the screen ----

    void command(String c) {
        ex.execute(() -> {
            try {
                onCommand(c);
            } catch (Exception e) {
                showToast("Error: " + e.getMessage());
            }
        });
    }

    private void onCommand(String c) {
        if (c.equals("start")) startOnEngine();
        else if (c.equals("stop")) stopOnEngine();
        else if (c.equals("grip:relaxed") || c.equals("grip:squeeze")) {
            if (!connected) { showToast("Connect the sleeve first."); return; }
            String phase = c.substring(5);
            gripMessage = null; gripResult = null;
            if (phase.equals("relaxed")) gripDone = false;
            proc.gripCalibration(phase);
            if (proc.snapshot.calibrationState.equals("capturing_" + phase)) gripFlow = phase;
            else { gripResult = "failed"; gripMessage = reasonText(proc.snapshot.calibrationReason, "Do the relax step first."); }
        } else if (c.equals("grip:cancel")) { proc.gripCalibration("cancel"); }
        else if (c.startsWith("tool:")) {
            String ph = c.substring(5);
            if (ph.equals("cancel")) { proc.toolCalibration("cancel"); return; }
            if (ph.equals("reset")) {
                proc.toolCalibration("reset");
                toolDone.clear(); toolMessage = null; toolFailed = false;
                showToast("Tool thresholds back to the defaults");
                return;
            }
            if (!connected || toolPhase != null) return;
            if (ph.equals("still")) toolDone.clear();
            toolMessage = null; toolFailed = false;
            proc.toolCalibration(ph);
            if (proc.toolCalState().equals("capturing_" + ph)) toolPhase = ph;
        } else if (c.startsWith("source:")) {
            setting(() -> { source = c.substring(7); prefs.edit().putString("source", source).apply(); });
        } else if (c.startsWith("cpg:")) {
            setting(() -> { countsPerG = Double.parseDouble(c.substring(4)); prefs.edit().putFloat("countsPerG", (float) countsPerG).apply(); });
        } else if (c.equals("day:clear")) {           // developer: start the demo again from zero
            dayEnergy = 0; dayContact = 0; dayTool = 0; shownWarnings.clear();
            saveDay();
            showToast("Today's points cleared");
        } else if (c.startsWith("speed:")) {
            demoSpeed = Math.max(1, Math.min(240, Double.parseDouble(c.substring(6))));
            prefs.edit().putFloat("demoSpeed", (float) demoSpeed).apply();
            showToast(demoSpeed > 1 ? "Demo: 1 minute of tool use counts as " + Math.round(demoSpeed) + " minutes" : "Demo speed off: real time");
        } else if (c.startsWith("limit:")) {
            // a supervisor can LOWER one worker's daily limit (HSE points); never above the limit value (400)
            personalLimit = Math.max(25, Math.min(Exposure.ELV_POINTS, Double.parseDouble(c.substring(6))));
            proc.setPersonalLimit(personalLimit);
            prefs.edit().putFloat("personalLimit", (float) personalLimit).apply();
        } else if (c.startsWith("demo:")) {
            if (demo == null) return;
            String[] kv = c.substring(5).split("=");
            boolean on = kv.length > 1 && kv[1].equals("1");
            if (kv[0].equals("squeeze")) demo.squeeze = on;
            else if (kv[0].equals("tool")) demo.tool = on;
            else if (kv[0].equals("arm")) demo.armMoving = on;
        }
    }

    /** A setting that changes the processing: restart the link and the processor. */
    private void setting(Runnable change) {
        boolean was = running;
        stopOnEngine();
        change.run();
        lastE = null; lastC = null; lastTool = null;   // today's totals carry on with the new processor
        proc = newProcessor();
        gripFlow = null; gripDone = false; toolPhase = null;
        if (was) startOnEngine();
    }

    // ---- state for the screen ----

    private JSONObject build(Processor.Snapshot s, double t) throws Exception {
        JSONObject o = new JSONObject();
        boolean fresh = connected && t - lastLineAt <= 1 && s.staleStreams.isEmpty();
        o.put("running", running).put("connected", connected).put("fresh", fresh).put("source", source);
        o.put("dataSource", s.source == null ? JSONObject.NULL : s.source);
        o.put("linkDetail", linkDetail).put("linkError", linkError == null ? JSONObject.NULL : linkError);
        o.put("age", lastLineAt < 0 ? JSONObject.NULL : t - lastLineAt).put("linesPerS", linesPerS);
        o.put("device", s.deviceId == null ? JSONObject.NULL : s.deviceId).put("firmware", s.firmware == null ? JSONObject.NULL : s.firmware);
        o.put("records", s.records).put("lost", s.missingRecords);
        // grip
        Double act = null;
        if (fresh && (s.activationStatus.equals("ok") || s.activationStatus.equals("degraded")) && s.activation != null) act = s.activation;
        o.put("activation", act == null ? JSONObject.NULL : act);
        o.put("envelope", fresh && s.envelope != null ? s.envelope : JSONObject.NULL);
        o.put("gripCalibrated", s.calibrationState.equals("calibrated"));
        Emg.GripCalibration g = proc.gripCalibrationActive();
        o.put("grip", new JSONObject()
                .put("flow", gripFlow == null ? JSONObject.NULL : gripFlow)
                .put("state", s.calibrationState)
                .put("left", s.calibrationLeftS == null ? JSONObject.NULL : s.calibrationLeftS)
                .put("total", s.calibrationDurationS)
                .put("done", gripDone).put("message", gripMessage == null ? JSONObject.NULL : gripMessage)
                .put("result", gripResult == null ? JSONObject.NULL : gripResult)
                .put("relaxed", g == null ? JSONObject.NULL : g.relaxed).put("squeeze", g == null ? JSONObject.NULL : g.squeeze));
        // vibration
        boolean vibOk = fresh && (s.vibrationStatus.equals("ok") || s.vibrationStatus.equals("degraded"));
        o.put("vibration", vibOk && s.vibration != null ? s.vibration : JSONObject.NULL).put("vibrationClipped", s.vibrationClipped);
        // tool
        ToolUse.State tl = s.tool;
        JSONObject tj = new JSONObject();
        tj.put("state", fresh ? tl.state : "unknown").put("band", nz(tl.toolBand)).put("arm", nz(tl.motionBand))
                .put("hz", nz(tl.dominantHz)).put("periodicity", nz(tl.concentration))
                .put("toolTime", toolToday()).put("bout", tl.boutS).put("bouts", tl.bouts).put("longest", tl.longestBoutS)
                .put("calibrated", tl.calibrationSource.equals("calibrated"));
        JSONArray warn = new JSONArray();
        for (String w : warnings(s)) warn.put(warningText(w));
        tj.put("warnings", warn);
        o.put("tool", tj);
        JSONObject tc = new JSONObject();
        tc.put("phase", toolPhase == null ? JSONObject.NULL : toolPhase)
                .put("left", tl.captureLeftS == null ? JSONObject.NULL : tl.captureLeftS)
                .put("total", toolPhase == null ? 1 : proc.toolCaptureDuration(toolPhase))
                .put("done", new JSONArray(toolDone)).put("failed", toolFailed)
                .put("message", toolMessage == null ? JSONObject.NULL : toolMessage)
                .put("result", proc.toolCalResult() == null ? JSONObject.NULL : proc.toolCalResult().toJson());
        o.put("toolCal", tc);
        // exposure
        Exposure.Snapshot x = s.exposure;
        double energy = energyToday(), points = Exposure.pointsFromEnergy(energy);
        Double aNow = fresh && Boolean.TRUE.equals(x.counting) ? x.aHvNow : null;
        o.put("exposure", new JSONObject().put("points", points).put("a8", Exposure.a8FromEnergy(energy, 8 * 3600))
                .put("contact", contactToday()).put("limit", x.personalLimit).put("action", Exposure.EAV_POINTS)
                .put("counting", aNow != null)
                .put("pointsPerHour", aNow == null ? JSONObject.NULL : 2 * aNow * aNow)
                // real time left on the phone's clock (demo: divided by the speed)
                .put("hoursToAction", nz(scaled(Exposure.hoursUntil(points, Exposure.EAV_POINTS, aNow), demoSpeed)))
                .put("hoursToLimit", nz(scaled(Exposure.hoursUntil(points, x.personalLimit, aNow), demoSpeed)))
                .put("status", x.status).put("reasons", new JSONArray(x.reasons))
                .put("gripUnknown", x.gripUnknownS));
        o.put("settings", new JSONObject().put("source", source).put("countsPerG", countsPerG).put("limit", personalLimit)
                .put("demoSpeed", demoSpeed));
        o.put("events", new JSONArray(log));
        JSONArray hist = new JSONArray();
        try {
            JSONObject days = new JSONObject(prefs.getString("days", "{}"));
            java.util.List<String> keys = new java.util.ArrayList<>();
            for (java.util.Iterator<String> it = days.keys(); it.hasNext(); ) keys.add(it.next());
            java.util.Collections.sort(keys, java.util.Collections.reverseOrder());
            for (String k : keys) hist.put(new JSONObject(days.getJSONObject(k).toString()).put("date", k));
        } catch (Exception ignored) {}
        o.put("history", hist).put("day", day);
        o.put("toast", toast == null ? JSONObject.NULL : toast).put("toastId", toastId);
        return o;
    }

    /** Break and daily tool-time reminders, on the (demo-scaled) clock the screen shows. */
    private java.util.List<String> warnings(Processor.Snapshot s) {
        java.util.List<String> w = new java.util.ArrayList<>();
        if ("tool".equals(s.tool.state) && s.tool.boutS * demoSpeed >= 20 * 60) w.add("continuous_tool_use_long");
        if (dayTool >= 2 * 3600) w.add("session_tool_time_high");
        else if (dayTool >= 0.8 * 2 * 3600) w.add("session_tool_time_approaching");
        return w;
    }

    private static Double scaled(Double hours, double speed) { return hours == null ? null : hours / speed; }

    private static Object nz(Double v) { return v == null ? JSONObject.NULL : v; }

    static String warningText(String w) {
        switch (w) {
            case "continuous_tool_use_long": return "Tool in use for over 20 minutes without a break. Take a break.";
            case "session_tool_time_approaching": return "Tool time this shift is approaching 2 hours.";
            case "session_tool_time_high": return "Tool time this shift passed 2 hours.";
            default: return w;
        }
    }

    static String reasonText(String reason, String fallback) {
        if (reason == null) return fallback;
        switch (reason) {
            case "insufficient_separation": return "The squeeze was not clearly stronger than the relaxed reading. Squeeze harder, or check the pads, and try again.";
            case "insufficient_data": return "Not enough clean signal was captured. Check the electrodes and try again.";
            case "clipped": return "The signal hit the top of the input range too often. Squeeze a little less hard and try again.";
            case "interrupted_stale": return "The Wi-Fi link paused during the capture (not your electrodes). Just try again.";
            case "interrupted_gap": return "Data was lost during the capture (Wi-Fi). Just try again.";
            case "interrupted_boot_change": return "The sleeve restarted during calibration. Start again.";
            case "cancelled": return "Calibration cancelled.";
            case "emg_not_configured": return "No muscle signal yet. Wait for the sleeve's data, then start.";
            case "relaxed_reference_missing": return "Do the relaxed step first.";
            case "movement_looks_like_tool": return "Some normal movement looked like steady tool vibration. Repeat step 2 with the tool fully off, or hold the tool more firmly in step 3.";
            case "tool_not_reliably_detected": return "The tool was not clearly different from normal movement. Hold the running tool firmly by its handle for the whole capture.";
            case "no_tool_vibration_detected": return "No vibration was seen during step 3. Is the tool on, and the sensor on the same arm?";
            case "no_movement_detected": return "Hardly any movement during step 2. Move more: handshakes, waving, swinging.";
            case "interrupted_data_gap": return "Data dropped during the capture (Wi-Fi). Just try again.";
            case "movement_capture_required_first": return "Do step 2 (normal movement) before step 3.";
            default: return reason.replace('_', ' ');
        }
    }
}
