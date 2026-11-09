package com.haven.app;

import android.content.Context;
import android.content.SharedPreferences;

import com.haven.app.engine.Emg;
import com.haven.app.engine.Event;
import com.haven.app.engine.Exposure;
import com.haven.app.engine.Processor;
import com.haven.app.engine.ToolUse;

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
 * processor (com.haven.app.engine), the grip and tool calibrations, the exposure score and tool time for the
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
    private volatile String tvState = "{}";      // the small snapshot the TV view streams (TvServer)

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
    // bumped by every grip/tool capture command: the page ignores results until it sees its own attempt, so the
    // previous attempt's result (e.g. an old failure) is never shown for a new one
    private int gripAttempt, toolAttempt;
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
    // calibration never counts as exposure: nothing is added while setup is open or a capture runs, nor for a
    // few seconds after (the tool detector holds "tool" for about 3 s after the vibration stops)
    private double setupPingAt = -1e9, quietUntil;
    private static final double QUIET_AFTER_S = 5;
    // DEMO: a massage gun for a 2-minute demo. The measured vibration and the formula are unchanged; only the
    // clock runs faster: each real second of tool use counts as `demoSpeed` seconds (60 = a minute counts as
    // an hour). Shown on the Today screen while it is on.
    private double demoSpeed;
    private boolean gripGate;
    // the Overview over time: the highest demo speed that added points today (stored on the archived day, so a
    // demo-speed day is labelled), points added in each hour of today, and the "days" log cached as the JSON
    // the screen gets (re-read only when the log changes: up to 400 days)
    private double dayDemo;
    private final double[] dayHours = new double[24];
    private int historyVersion;
    private int historyBuilt = -1;
    private JSONObject historyDays = new JSONObject();
    private String historyJson = "[]";

    // alarms: each level fires once a day; the screen shows it until the wearer taps OK
    interface Listener { void alert(String kind, String title, String text); void silence(); }
    volatile Listener listener;
    private boolean firedAction, firedLimit;
    private String alarm;                         // null | action | limit (shown on the screen until acknowledged)
    // the sleeve's status light (NeoPixel on board W) shows the Today ring's colour
    private String ledSent;
    private Socket ledSocket;
    private double ledAt;
    private long alarmId;
    private final ArrayDeque<String> log = new ArrayDeque<>();
    private final Set<String> shownWarnings = new HashSet<>();
    private String toast;
    private long toastId;

    private Sleeve(Context c) {
        prefs = c.getSharedPreferences("haven", Context.MODE_PRIVATE);
        source = prefs.getString("source", "sleeve");
        countsPerG = prefs.getFloat("countsPerG", 4096);
        personalLimit = prefs.getFloat("personalLimit", 400);
        demoSpeed = prefs.getFloat("demoSpeed", 480);
        // a light hold on a massage gun is often under 10 % of a hard squeeze, so for the demo points count
        // whenever the tool is detected (exposure.py's tool_on total); the grip rule is a Developer option
        gripGate = prefs.getBoolean("gripGate", false);
        proc = newProcessor();
        loadDay();
        ex.scheduleAtFixedRate(this::tick, 100, 50, TimeUnit.MILLISECONDS);   // the page reads the state 10x a second
        TvServer.start(c, this);   // the TV view on this phone's Wi-Fi/hotspot: http://<phone>:8080/
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
                firedAction = t.optBoolean("firedAction");
                firedLimit = t.optBoolean("firedLimit");
                dayDemo = t.optDouble("demoSpeed", 0);
                JSONArray h = t.optJSONArray("hours");
                for (int i = 0; h != null && i < 24 && i < h.length(); i++) dayHours[i] = h.optDouble(i, 0);
            } else if (t.has("date")) {
                archive(t.optString("date"), t.optDouble("energy", 0), t.optDouble("tool", 0),
                        t.optDouble("contact", 0), t.optDouble("demoSpeed", 0));
            }
        } catch (Exception ignored) {}
    }

    private double toolToday() { return dayTool; }
    private double energyToday() { return dayEnergy; }
    private double contactToday() { return dayContact; }

    private void saveDay() {
        try {
            prefs.edit().putString("today", new JSONObject().put("date", day).put("energy", energyToday())
                    .put("contact", contactToday()).put("tool", toolToday())
                    .put("firedAction", firedAction).put("firedLimit", firedLimit)
                    .put("demoSpeed", dayDemo).put("hours", new JSONArray(dayHours)).toString()).apply();
        } catch (Exception ignored) {}
    }

    private void archive(String date, double energy, double tool, double contact, double demo) {
        try {
            JSONObject days = new JSONObject(prefs.getString("days", "{}"));
            JSONObject d = new JSONObject().put("points", Exposure.pointsFromEnergy(energy))
                    .put("a8", Exposure.a8FromEnergy(energy, 8 * 3600)).put("tool", tool).put("limit", personalLimit)
                    .put("contact", contact);
            if (demo > 1) d.put("demoSpeed", demo);       // recorded at demo speed: labelled on the Overview
            days.put(date, d);
            saveDays(days);                               // every day is kept on the phone
        } catch (Exception ignored) {}
    }

    /** Every write of the "days" log goes here (all days are kept locally); the screen's copy is rebuilt once. */
    private void saveDays(JSONObject days) {
        prefs.edit().putString("days", days.toString()).apply();
        historyVersion++;
    }

    /** The newest 400 days as the screen gets them, newest first, with each day's date; rebuilt only after a change. */
    private String historyJson() {
        if (historyBuilt == historyVersion) return historyJson;
        historyBuilt = historyVersion;
        historyDays = History.parse(prefs.getString("days", "{}"));
        JSONArray hist = new JSONArray();
        java.util.List<String> keys = History.newest(historyDays, History.SCREEN_DAYS);
        try {
            for (String k : keys) hist.put(new JSONObject(historyDays.getJSONObject(k).toString()).put("date", k));
        } catch (Exception ignored) {}
        historyJson = hist.toString();
        return historyJson;
    }

    private void resetToday() {
        dayEnergy = 0; dayContact = 0; dayTool = 0; dayDemo = 0;
        java.util.Arrays.fill(dayHours, 0);
    }

    /** Called every tick: a new date archives today and starts a new day. */
    private void logDay(double t) {
        String now = today();
        if (!now.equals(day)) {
            archive(day, energyToday(), toolToday(), contactToday(), dayDemo);
            day = now;
            resetToday();
            firedAction = false; firedLimit = false; alarm = null;
            shownWarnings.clear();
            note("New day started");
        }
        // add what the engine added since the last tick, at the demo speed (never during calibration)
        Exposure.Snapshot x = proc.snapshot.exposure;
        double e = x.energy, c = x.contactS, tt = proc.snapshot.tool.sessionToolS;
        boolean calibrating = gripFlow != null || toolPhase != null || t - setupPingAt < 2.5;
        if (calibrating) quietUntil = t + QUIET_AFTER_S;
        if (lastE != null && t >= quietUntil) {
            double added = Math.max(0, e - lastE) * demoSpeed;
            if (added > 0) {
                dayHours[java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY)] += added;
                if (demoSpeed > 1) dayDemo = Math.max(dayDemo, demoSpeed);
            }
            dayEnergy += Math.max(0, e - lastE) * demoSpeed;
            dayContact += Math.max(0, c - lastC) * demoSpeed;
            dayTool += Math.max(0, tt - lastTool) * demoSpeed;
        }
        lastE = e; lastC = c; lastTool = tt;
        checkAlarms();
        led(t);
        if (t - lastSaved > 10) { saveDay(); lastSaved = t; }
    }

    /**
     * Same colours as the Today ring: green under the action level, amber from 100 points, red at the daily limit,
     * and past it a slow fade between the snake's bright and deep red. Sent on change, and every 3 s so a
     * reconnected sleeve picks it up (the light falls back to "looking for the phone" after 15 s without one).
     */
    private void led(double t) {
        Socket sock = current;
        if (sock == null || source.equals("demo")) return;
        double pts = Exposure.pointsFromEnergy(dayEnergy);
        String line = pts >= personalLimit * 1.02 ? "VGLED 1 breathe ff3048 5c000f 2400"
                : pts >= personalLimit ? "VGLED 1 solid c8102e"
                : pts >= Exposure.EAV_POINTS ? "VGLED 1 solid b86e00"
                : "VGLED 1 solid 12804a";
        if (line.equals(ledSent) && sock == ledSocket && t - ledAt < 3) return;
        try {
            sock.getOutputStream().write((line + "\n").getBytes(StandardCharsets.US_ASCII));
            ledSent = line; ledSocket = sock; ledAt = t;
        } catch (Exception ignored) {
            ledSent = null;                       // the reader thread notices the closed socket
        }
    }

    private void checkAlarms() {
        double pts = Exposure.pointsFromEnergy(dayEnergy), limit = personalLimit;   // the setting, never the not-yet-filled snapshot
        if (pts <= 0) return;
        if (!firedLimit && pts >= limit) {
            firedLimit = true; firedAction = true; alarm = "limit"; alarmId++;
            raise("limit", "Daily limit reached", "You have reached " + Math.round(limit) + " points. Stop using vibrating tools for the rest of the day.");
            saveDay();
        } else if (!firedAction && pts >= Exposure.EAV_POINTS) {
            firedAction = true; alarm = "action"; alarmId++;
            raise("action", "Action level reached", "100 points. Your employer has to cut your vibration today: other tasks, other tools or more breaks.");
            saveDay();
        }
    }

    private Processor newProcessor() {
        Processor p = new Processor(countsPerG);
        p.setPersonalLimit(personalLimit);
        p.setGripGate(gripGate);
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
    String tvJson() { return tvState; }

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
            // the only alarms are the action level and the daily limit (checkAlarms); no other reminder sounds or pops up
            String b = build(s, t).toString();
            tvState = buildTv(s, t).toString();
            // the day log (up to 400 days) is serialized once per change, not 20 times a second
            state = b.substring(0, b.length() - 1) + ",\"history\":" + historyJson() + "}";
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

    private void raise(String kind, String title, String text) {
        Listener l = listener;
        if (l != null) l.alert(kind, title, text);
        note(title);
    }

    /** One line for the ongoing notification. */
    String summary() {
        double pts = Exposure.pointsFromEnergy(dayEnergy);
        String tool = dayTool < 60 ? "under a minute" : (dayTool >= 3600 ? (int) (dayTool / 3600) + " h " : "") + (int) (dayTool / 60 % 60) + " min";
        return (connected ? "" : "Waiting for the sleeve. ") + "Today: " + Math.round(pts) + " points, tool time " + tool
                + (demoSpeed > 1 ? " (demo speed)" : "");
    }

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
        if (c.equals("setup:open")) { setupPingAt = now(); return; }   // the setup screen is open (sent every second)
        if (c.equals("start")) startOnEngine();
        else if (c.equals("stop")) stopOnEngine();
        else if (c.equals("grip:relaxed") || c.equals("grip:squeeze")) {
            String phase = c.substring(5);
            gripAttempt++;
            if (!connected) { gripResult = "failed"; gripMessage = "The sleeve is not sending. Switch it on and wait until it connects, then try again."; return; }
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
            if (toolPhase != null) return;
            toolAttempt++;
            if (!connected) { toolFailed = true; toolMessage = "The sleeve is not sending. Switch it on and wait until it connects, then try again."; return; }
            if (ph.equals("still")) toolDone.clear();
            toolMessage = null; toolFailed = false;
            proc.toolCalibration(ph);
            if (proc.toolCalState().equals("capturing_" + ph)) toolPhase = ph;
        } else if (c.startsWith("source:")) {
            setting(() -> { source = c.substring(7); prefs.edit().putString("source", source).apply(); });
        } else if (c.startsWith("cpg:")) {
            setting(() -> { countsPerG = Double.parseDouble(c.substring(4)); prefs.edit().putFloat("countsPerG", (float) countsPerG).apply(); });
        } else if (c.equals("alarm:ack")) {
            alarm = null;
            Listener l = listener;
            if (l != null) l.silence();
        } else if (c.equals("day:clear")) {           // developer: start the demo again from zero
            resetToday(); shownWarnings.clear();
            firedAction = false; firedLimit = false; alarm = null;
            saveDay();
            showToast("Today's points cleared");
        } else if (c.equals("history:sample") || c.equals("history:clearSample") || c.equals("history:resetDemo")) {
            // developer: a year of SAMPLE days for the Overview's Month and Year views, marked "sample"; never
            // today and never over a real day. Reset demo = clear today + fresh sample year.
            JSONObject days = History.parse(prefs.getString("days", "{}"));
            int removed = History.clearSample(days);
            int added = c.equals("history:clearSample") ? 0 : History.addSample(days, day, History.SAMPLE_DAYS);
            saveDays(days);
            if (c.equals("history:resetDemo")) {
                resetToday(); shownWarnings.clear();
                firedAction = false; firedLimit = false; alarm = null;
                saveDay();
                showToast("Demo reset: today cleared, " + added + " sample days added");
            } else showToast(added > 0 ? added + " sample days added (marked as sample)" : removed + " sample days removed");
        } else if (c.equals("week:sample") || c.equals("week:clearSample")) {
            // developer: SAMPLE days for demos, marked "sample" and shown as sample data on the Overview
            try {
                JSONObject days = new JSONObject(prefs.getString("days", "{}"));
                java.util.List<String> drop = new java.util.ArrayList<>();
                for (java.util.Iterator<String> it = days.keys(); it.hasNext(); ) {
                    String k = it.next();
                    if (days.getJSONObject(k).optBoolean("sample")) drop.add(k);
                }
                for (String k : drop) days.remove(k);
                if (c.equals("week:sample")) {
                    double[][] v = {{132, 5400}, {41, 1900}, {218, 7300}, {86, 3600}, {405, 11800}, {12, 600}};   // points, tool s
                    java.util.Calendar cal = java.util.Calendar.getInstance();
                    java.text.SimpleDateFormat f = new java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US);
                    for (double[] d : v) {
                        cal.add(java.util.Calendar.DAY_OF_MONTH, -1);
                        String k = f.format(cal.getTime());
                        if (!days.has(k)) days.put(k, new JSONObject().put("points", d[0]).put("tool", d[1])
                                .put("a8", 2.5 * Math.sqrt(d[0] / 100)).put("limit", 400).put("sample", true));
                    }
                }
                saveDays(days);
                showToast(c.equals("week:sample") ? "Sample week added (marked as sample)" : "Sample days removed");
            } catch (Exception ignored) {}
        } else if (c.startsWith("gripgate:")) {
            gripGate = c.endsWith("1");
            proc.setGripGate(gripGate);
            prefs.edit().putBoolean("gripGate", gripGate).apply();
            showToast(gripGate ? "Points count only while gripping" : "Points count whenever the tool is detected");
        } else if (c.startsWith("speed:")) {
            demoSpeed = Math.max(1, Math.min(960, Double.parseDouble(c.substring(6))));
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

    // ---- state for the TV view (TvServer) ----

    /** Only what the TV meters and their proof strip need, 20 times a second; never the history. */
    private JSONObject buildTv(Processor.Snapshot s, double t) throws Exception {
        JSONObject o = new JSONObject();
        boolean fresh = connected && t - lastLineAt <= 1 && s.staleStreams.isEmpty();
        boolean actOk = fresh && (s.activationStatus.equals("ok") || s.activationStatus.equals("degraded"));
        boolean vibOk = fresh && (s.vibrationStatus.equals("ok") || s.vibrationStatus.equals("degraded"));
        o.put("running", running).put("connected", connected).put("fresh", fresh);
        o.put("source", source).put("dataSource", s.source == null ? JSONObject.NULL : s.source);
        o.put("age", lastLineAt < 0 ? JSONObject.NULL : t - lastLineAt);
        o.put("device", s.deviceId == null ? JSONObject.NULL : s.deviceId);
        o.put("firmware", s.firmware == null ? JSONObject.NULL : s.firmware);
        o.put("emgSensor", s.emgSensor == null ? JSONObject.NULL : s.emgSensor);
        o.put("imuSensor", s.imuSensor == null ? JSONObject.NULL : s.imuSensor);
        o.put("emgSamples", s.emgSamples).put("imuSamples", s.imuSamples);
        o.put("records", s.records).put("lost", s.missingRecords).put("boot", s.bootId == null ? JSONObject.NULL : s.bootId);
        o.put("activation", actOk && s.activation != null ? s.activation : JSONObject.NULL);
        o.put("gripState", s.calibrationState).put("gripFlow", gripFlow == null ? JSONObject.NULL : gripFlow);
        o.put("gripReason", s.calibrationReason == null ? JSONObject.NULL : s.calibrationReason).put("gripAttempt", gripAttempt);
        o.put("emgSaturation", s.emgSaturation).put("envelope", fresh && s.envelope != null ? s.envelope : JSONObject.NULL);
        o.put("vibration", vibOk && s.vibration != null ? s.vibration : JSONObject.NULL);
        o.put("tool", fresh ? s.tool.state : "unknown").put("toolHz", nz(s.tool.dominantHz));
        o.put("t", t);
        return o;
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
                .put("relaxed", g == null ? JSONObject.NULL : g.relaxed).put("squeeze", g == null ? JSONObject.NULL : g.squeeze)
                .put("attempt", gripAttempt).put("reason", s.calibrationReason == null ? JSONObject.NULL : s.calibrationReason)
                .put("saturation", fresh ? s.emgSaturation : JSONObject.NULL));
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
        tc.put("attempt", toolAttempt).put("phase", toolPhase == null ? JSONObject.NULL : toolPhase)
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
                .put("contact", contactToday()).put("limit", personalLimit).put("action", Exposure.EAV_POINTS)
                .put("counting", aNow != null)
                .put("pointsPerHour", aNow == null ? JSONObject.NULL : 2 * aNow * aNow)
                // real time left on the phone's clock (demo: divided by the speed)
                .put("hoursToAction", nz(scaled(Exposure.hoursUntil(points, Exposure.EAV_POINTS, aNow), demoSpeed)))
                .put("hoursToLimit", nz(scaled(Exposure.hoursUntil(points, x.personalLimit, aNow), demoSpeed)))
                .put("status", x.status).put("reasons", new JSONArray(x.reasons))
                .put("gripUnknown", x.gripUnknownS)
                .put("paused_for_setup", now() < quietUntil)
                // developer: minutes of tool use at the vibration measured now until the limit, at the demo speed
                .put("demoMinutesToLimit", x.aHvNow == null || x.aHvNow <= 0 ? JSONObject.NULL
                        : (x.personalLimit - Math.min(points, x.personalLimit)) / (2 * x.aHvNow * x.aHvNow) * 60 / demoSpeed));
        o.put("alarm", alarm == null ? JSONObject.NULL : alarm).put("alarmId", alarmId);
        o.put("settings", new JSONObject().put("source", source).put("countsPerG", countsPerG).put("limit", personalLimit)
                .put("demoSpeed", demoSpeed).put("gripGate", gripGate));
        o.put("events", new JSONArray(log));
        // "history" (the day log, newest first) is added by tick() from the cached copy
        o.put("day", day).put("historyVersion", historyVersion);
        // the Overview over time: today's points hour by hour, today's demo speed, and the last 12 months
        // (energy-mean daily A(8) per month as average points per working day; never a sum of days)
        JSONArray hours = new JSONArray();
        for (double h : dayHours) hours.put(History.round(Exposure.pointsFromEnergy(h), 2));
        o.put("todayHours", hours).put("dayDemoSpeed", dayDemo);
        historyJson();
        JSONObject todayEntry = new JSONObject().put("points", points).put("a8", Exposure.a8FromEnergy(energy, 8 * 3600))
                .put("tool", toolToday()).put("limit", personalLimit).put("demoSpeed", dayDemo);
        o.put("months", History.months(historyDays, day, todayEntry, 12));
        o.put("toast", toast == null ? JSONObject.NULL : toast).put("toastId", toastId);
        o.put("tvUrls", new JSONArray(TvServer.urls()));
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
            case "noisy_at_rest": return "The muscle signal is noisy even with your arm relaxed, so a squeeze cannot stand out. Press all three pads firmly onto the skin, check the cable snaps, keep the cable still (tape it to the arm), then try again.";
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
