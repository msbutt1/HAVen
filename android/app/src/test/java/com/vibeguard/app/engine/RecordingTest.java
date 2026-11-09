package com.vibeguard.app.engine;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * The Java engine against the Python reference processor, on REAL recordings from the sleeve (board W,
 * 2026-11-08): the same lines and calibration presses in, the same results out. The fixtures are shared with
 * the Swift engine's tests (ios/EngineTests/Fixtures, made by make_fixtures.py).
 */
public class RecordingTest {
    static File fixtures() {
        for (String p : new String[]{"../../ios/EngineTests/Fixtures", "../ios/EngineTests/Fixtures", "ios/EngineTests/Fixtures"}) {
            File f = new File(p);
            if (f.isDirectory()) return f;
        }
        throw new AssertionError("fixtures not found (ios/EngineTests/Fixtures)");
    }

    static JSONObject load(String name) throws Exception {
        return new JSONObject(new String(Files.readAllBytes(new File(fixtures(), name).toPath()), StandardCharsets.UTF_8));
    }

    static final class Run {
        final Map<String, Map<String, Object>> got = new HashMap<>();
        final List<Event> events = new ArrayList<>();
        final Processor proc = new Processor(4096);
    }

    static Run run(JSONObject f) throws Exception {
        Run r = new Run();
        JSONArray lines = f.getJSONArray("lines");
        JSONObject cmds = f.getJSONObject("commands");
        double clock = 0;
        for (int i = 0; i < lines.length(); i++) {
            String line = lines.getString(i);
            Records.Record rec = Records.parse(line);
            assertNotNull("did not parse", rec);
            if (rec.batch != null) clock = Math.max(clock, rec.batch.times[rec.batch.times.length - 1] / 1e6);
            r.proc.feed(rec, clock);
            Processor.Snapshot s = r.proc.snapshot;
            if (rec.batch != null) {
                Map<String, Object> m = new HashMap<>();
                if (rec.batch.stream.equals("emg")) {
                    m.put("envelope", s.envelope); m.put("activation", s.activation); m.put("calibration", s.calibrationState);
                    r.got.put("emg:" + rec.seq, m);
                } else {
                    m.put("vibration", s.vibration);
                    r.got.put("imu:" + rec.seq, m);
                    Map<String, Object> t = new HashMap<>();
                    t.put("state", s.tool.state); t.put("tool", s.tool.toolBand); t.put("motion", s.tool.motionBand);
                    t.put("dominant_hz", s.tool.dominantHz); t.put("conc", s.tool.concentration); t.put("session_tool_s", s.tool.sessionToolS);
                    r.got.put("tool:" + rec.seq, t);
                    Map<String, Object> ex = new HashMap<>();
                    Exposure.Snapshot x = s.exposure;
                    ex.put("points", x.points); ex.put("a8", x.a8); ex.put("contact_s", x.contactS);
                    ex.put("tool_on_points", x.toolOnPoints); ex.put("level", x.level); ex.put("counting", x.counting);
                    r.got.put("exposure:" + rec.seq, ex);
                    Map<String, Object> sc = new HashMap<>();
                    sc.put("cumulative", s.scoreTotal);
                    r.got.put("score:" + rec.seq, sc);
                }
            }
            if (!rec.type.equals("hello") && cmds.has(String.valueOf(rec.seq))) {
                JSONArray cs = cmds.getJSONArray(String.valueOf(rec.seq));
                for (int k = 0; k < cs.length(); k++) {
                    String c = cs.getString(k);
                    if (c.startsWith("toolcal:")) r.proc.toolCalibration(c.substring(8)); else r.proc.gripCalibration(c);
                }
            }
            r.events.addAll(r.proc.takeEvents());
        }
        return r;
    }

    /** field -> {max abs error, values compared, missing}; asserts tool and calibration states match. */
    static Map<String, double[]> compare(Run r, JSONObject f) throws Exception {
        Map<String, double[]> worst = new HashMap<>();
        int stateMismatch = 0;
        JSONArray exp = f.getJSONArray("expected");
        String[] keys = {"envelope", "activation", "vibration", "tool", "motion", "conc", "dominant_hz", "cumulative", "session_tool_s",
                "points", "a8", "contact_s", "tool_on_points"};
        for (int i = 0; i < exp.length(); i++) {
            JSONObject e = exp.getJSONObject(i);
            Map<String, Object> g = r.got.get(e.getString("stream") + ":" + e.getLong("seq"));
            if (g == null) continue;
            for (String k : keys) {
                if (!e.has(k) || e.isNull(k) || !g.containsKey(k)) continue;
                double want = e.getDouble(k);
                double[] w = worst.computeIfAbsent(k, x -> new double[3]);
                Object have = g.get(k);
                if (!(have instanceof Double)) { w[2]++; continue; }
                w[0] = Math.max(w[0], Math.abs(want - (Double) have));
                w[1]++;
            }
            if (e.getString("stream").equals("tool") && !e.getString("state").equals(g.get("state"))) stateMismatch++;
            if (e.getString("stream").equals("emg") && !e.getString("calibration").equals(g.get("calibration"))) stateMismatch++;
            if (e.getString("stream").equals("exposure")) {
                if (!e.getString("level").equals(g.get("level"))) stateMismatch++;
                Object want = e.isNull("counting") ? null : e.getBoolean("counting");
                if (want == null ? g.get("counting") != null : !want.equals(g.get("counting"))) stateMismatch++;
            }
        }
        assertEquals("tool or calibration state differs", 0, stateMismatch);
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, double[]> e : worst.entrySet())
            sb.append(String.format("%s: max err %.2g over %d (missing %d); ", e.getKey(), e.getValue()[0], (long) e.getValue()[1], (long) e.getValue()[2]));
        System.out.println(sb);
        return worst;
    }

    @Test
    public void gripCalibrationOnRealRecording() throws Exception {
        JSONObject f = load("grip_calibration.json");
        Run r = run(f);
        Map<String, double[]> w = compare(r, f);
        assertTrue(w.get("envelope")[1] > 100);
        assertTrue(w.get("envelope")[0] < 0.01);
        assertEquals(0, (long) w.get("envelope")[2]);
        assertTrue(w.get("activation")[0] < 0.001);
        assertTrue(r.events.stream().anyMatch(e -> e.kind == Event.Kind.CALIBRATION && "calibrated".equals(e.state)));
    }

    @Test
    public void toolCalibrationOnRealRecording() throws Exception {
        JSONObject f = load("tool_calibration.json");
        Run r = run(f);
        Map<String, double[]> w = compare(r, f);
        assertTrue(w.get("vibration")[0] < 0.001);
        assertTrue(w.get("tool")[0] < 0.002);
        assertTrue(w.get("motion")[0] < 0.002);
        assertTrue(w.get("conc")[0] < 0.002);
        assertEquals(0.0, w.get("dominant_hz")[0], 0.0);
        assertTrue("exposure compared", w.get("points")[1] > 100);
        assertTrue(w.get("points")[0] < 1e-4);
        assertTrue(w.get("contact_s")[0] < 1e-3);
        JSONObject want = null;
        JSONArray ev = f.getJSONArray("events");
        for (int i = 0; i < ev.length(); i++) if ("calibrated".equals(ev.getJSONObject(i).optString("state"))) want = ev.getJSONObject(i).getJSONObject("thresholds");
        Event last = null;
        for (Event e : r.events) if (e.kind == Event.Kind.TOOL_CALIBRATION) last = e;
        assertNotNull(last);
        assertEquals("calibrated", last.state);
        for (java.util.Iterator<String> it = want.keys(); it.hasNext(); ) { String k = it.next(); assertEquals(k, want.getDouble(k), last.result.thresholds.get(k), 0.002); }
        System.out.println("tool thresholds: " + last.result.thresholds);
    }

    @Test
    public void filtersMatchScipy() throws Exception {
        JSONObject f = load("filters.json");
        JSONArray x = f.getJSONArray("x");
        JSONArray cases = f.getJSONArray("cases");
        for (int c = 0; c < cases.length(); c++) {
            JSONObject k = cases.getJSONObject(c);
            JSONArray fr = k.getJSONArray("f");
            double fs = k.getDouble("fs");
            int n = k.getInt("order");
            Filters.Biquad[] s = k.getString("kind").equals("bp") ? Filters.bandpass(n, fr.getDouble(0), fr.getDouble(1), fs)
                    : Filters.highpass(n, fr.getDouble(0), fs);
            Filters.SOS flt = new Filters.SOS(s, 1);
            JSONArray y = k.getJSONArray("y");
            double maxErr = 0;
            for (int i = 0; i < x.length(); i++) maxErr = Math.max(maxErr, Math.abs(flt.step(x.getDouble(i), 0) - y.getDouble(i)));
            assertTrue(k.getString("kind") + " " + fr + ": " + maxErr, maxErr < 1e-9);
        }
    }
}
