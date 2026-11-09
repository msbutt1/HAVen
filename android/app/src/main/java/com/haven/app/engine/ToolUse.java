package com.haven.app.engine;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * Tool use vs normal arm use, tool time and warnings. Port of processing/tool_use.py (via the Swift engine).
 *
 * A 0.5 s window is a tool candidate when the tool band (20 Hz high-pass) is loud enough, strong compared with arm
 * movement (0.5-10 Hz), and periodic (power in a few frequency lines). "tool" needs candidates for 2 s with a steady
 * dominant frequency, so handshakes, waves and knocks never count. Thresholds are provisional team settings, or set
 * per wearer by the tool calibration. Not regulatory limits, not a dose.
 */
public final class ToolUse {
    public static final class Config implements Cloneable {
        double toolBandLowHz = 20, motionLowHz = 0.5, motionHighHz = 10;
        int order = 4;
        double windowS = 0.5;
        double onThreshold = 1.0, offThreshold = 0.6, toolToMotionMin = 0.5, concentrationMin = 0.0;
        double steadyMin = 0.6, steadyTolHz = 5.0;
        double onHoldS = 2.0, offHoldS = 3.0, maxStepS = 0.5;
        double continuousWarnS = 20 * 60, sessionWarnS = 2 * 3600, sessionNoticeFraction = 0.8;
        double calStillS = 5, calMovementS = 12, calToolS = 10, calDiscardS = 1;
        double calToolPassMin = 0.8, calMovementPassMax = 0.10;
        @Override public Config clone() { try { return (Config) super.clone(); } catch (CloneNotSupportedException e) { throw new AssertionError(e); } }
    }

    public static final class State {
        public String state = "unknown";             // tool | no_tool | unknown
        public boolean candidate;
        public Double toolBand, motionBand, dominantHz, concentration, steadiness;
        public double boutS, longestBoutS, sessionToolS, sessionObservedS;
        public int bouts;
        public List<String> warnings = new ArrayList<>();
        public String calibrationState = "uncalibrated", calibrationSource = "default";
        public Double captureLeftS;
    }

    /** Median and 95th percentile of one capture's windows. */
    public static final class Summary {
        public int windows;
        public double[] toolBand, motionBand, ratio, concentration;   // {median, p95}
    }

    public static final class CalResult {
        public Map<String, Summary> summaries = new HashMap<>();
        public double toolWindowsAccepted, movementWindowsAccepted;
        public int movementFalseBouts;
        public Map<String, Boolean> separates = new HashMap<>();
        public Map<String, Double> thresholds = new HashMap<>();

        public JSONObject toJson() throws JSONException {
            JSONObject o = new JSONObject();
            JSONObject th = new JSONObject();
            for (Map.Entry<String, Double> e : thresholds.entrySet()) th.put(e.getKey(), e.getValue());
            o.put("thresholds", th);
            o.put("tool_windows_accepted", toolWindowsAccepted);
            o.put("movement_windows_accepted", movementWindowsAccepted);
            o.put("movement_false_bouts", movementFalseBouts);
            JSONObject sm = new JSONObject();
            for (Map.Entry<String, Summary> e : summaries.entrySet()) {
                Summary s = e.getValue();
                JSONObject q = new JSONObject();
                q.put("windows", s.windows);
                q.put("tool_band", s.toolBand[0]); q.put("motion_band", s.motionBand[0]);
                q.put("ratio", s.ratio[0]); q.put("concentration", s.concentration[0]);
                sm.put(e.getKey(), q);
            }
            o.put("summaries", sm);
            return o;
        }

        public static CalResult fromJson(JSONObject o) throws JSONException {
            CalResult r = new CalResult();
            JSONObject th = o.getJSONObject("thresholds");
            for (Iterator<String> it = th.keys(); it.hasNext(); ) { String k = it.next(); r.thresholds.put(k, th.getDouble(k)); }
            r.toolWindowsAccepted = o.optDouble("tool_windows_accepted", 0);
            r.movementWindowsAccepted = o.optDouble("movement_windows_accepted", 0);
            r.movementFalseBouts = o.optInt("movement_false_bouts", 0);
            JSONObject sm = o.optJSONObject("summaries");
            if (sm != null) for (Iterator<String> it = sm.keys(); it.hasNext(); ) {
                String k = it.next();
                JSONObject q = sm.getJSONObject(k);
                Summary s = new Summary();
                s.windows = q.optInt("windows");
                s.toolBand = new double[]{q.optDouble("tool_band"), 0}; s.motionBand = new double[]{q.optDouble("motion_band"), 0};
                s.ratio = new double[]{q.optDouble("ratio"), 0}; s.concentration = new double[]{q.optDouble("concentration"), 0};
                r.summaries.put(k, s);
            }
            return r;
        }
    }

    private static final class Win {        // one window's features during a calibration capture
        final double dt, tool, motion, conc; final Double dom;
        Win(double dt, double tool, double motion, double conc, Double dom) { this.dt = dt; this.tool = tool; this.motion = motion; this.conc = conc; this.dom = dom; }
    }

    final Config defaults;
    Config cfg;
    private final double scale;
    Double rate;
    private Filters.SOS hp, bp;
    private int nWin;
    private double[] x0;
    private final ArrayDeque<double[]> toolBuf = new ArrayDeque<>(), motionBuf = new ArrayDeque<>();
    private double[] hann;
    private double[][] cosT, sinT;

    String state = "no_tool";
    double sessionToolS, sessionObservedS;
    int bouts;
    private double boutS, longestBoutS, candS, unobservedS;
    private final ArrayDeque<double[]> hist = new ArrayDeque<>();      // {dt, active}
    private final ArrayDeque<double[]> doms = new ArrayDeque<>();      // {dt, dom or NaN}
    private Double lastT;

    String calState = "uncalibrated", calSource = "default", lastFailure;
    CalResult calResult;
    private final Map<String, List<Win>> captures = new HashMap<>();
    private String calPhase;
    private double calElapsed;
    private List<Win> calBuf = new ArrayList<>();
    final List<Event> events = new ArrayList<>();

    ToolUse(Config config, double countsPerG) {
        defaults = config;
        cfg = config.clone();
        scale = 9.80665 / countsPerG;
    }

    void configure(double rate) {
        if (this.rate != null && this.rate == rate) return;
        this.rate = rate;
        hp = new Filters.SOS(Filters.highpass(cfg.order, cfg.toolBandLowHz, rate), 3);
        bp = new Filters.SOS(Filters.bandpass(2, cfg.motionLowHz, cfg.motionHighHz, rate), 3);
        nWin = Math.max(8, Filters.roundHalfEven(cfg.windowS * rate));
        int n = nWin, bins = n / 2 + 1;
        hann = new double[n];
        for (int i = 0; i < n; i++) hann[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1));   // numpy.hanning
        cosT = new double[bins][n];
        sinT = new double[bins][n];
        for (int k = 0; k < bins; k++) for (int i = 0; i < n; i++) {
            double a = 2 * Math.PI * ((long) k * i % n) / n;
            cosT[k][i] = Math.cos(a); sinT[k][i] = Math.sin(a);
        }
        reset("configured");
    }

    void reset(String reason) {
        x0 = null;
        if (hp != null) { hp.reset(); bp.reset(); }
        toolBuf.clear(); motionBuf.clear();
    }

    void process(double[][] rows) {
        if (hp == null) return;
        for (double[] r : rows) {
            if (r == null) { reset("missing_values"); continue; }
            double[] x = new double[3];
            for (int a = 0; a < 3; a++) x[a] = r[a] * scale;
            if (x0 == null) x0 = x.clone();
            double[] t = new double[3], m = new double[3];
            for (int a = 0; a < 3; a++) { t[a] = hp.step(x[a] - x0[a], a); m[a] = bp.step(x[a] - x0[a], a); }
            toolBuf.addLast(t); motionBuf.addLast(m);
            while (toolBuf.size() > 2 * nWin) { toolBuf.removeFirst(); motionBuf.removeFirst(); }
        }
    }

    /** {tool, motion, dominantHz (NaN if none), concentration}, or null before two windows of data. */
    private double[] features() {
        int n = nWin;
        if (rate == null || n == 0 || toolBuf.size() < 2 * n) return null;
        double[][] t = new double[n][], m = new double[n][];
        Iterator<double[]> it = toolBuf.descendingIterator(), im = motionBuf.descendingIterator();
        for (int i = n - 1; i >= 0; i--) { t[i] = it.next(); m[i] = im.next(); }
        double tool = 0, motion = 0;
        for (int a = 0; a < 3; a++) {
            double st = 0, sm = 0;
            for (int i = 0; i < n; i++) { st += t[i][a] * t[i][a]; sm += m[i][a] * m[i][a]; }
            tool += st / n; motion += sm / n;
        }
        int bins = n / 2 + 1;
        double[] spec = new double[bins];
        double[] w = new double[n];
        for (int a = 0; a < 3; a++) {
            for (int i = 0; i < n; i++) w[i] = t[i][a] * hann[i];
            for (int k = 0; k < bins; k++) {
                double re = 0, imv = 0;
                double[] c = cosT[k], s = sinT[k];
                for (int i = 0; i < n; i++) { re += w[i] * c[i]; imv -= w[i] * s[i]; }
                spec[k] += re * re + imv * imv;
            }
        }
        double dom = Double.NaN;
        if (bins > 1) {
            int best = 1;
            for (int k = 1; k < bins; k++) if (spec[k] > spec[best]) best = k;
            dom = best * rate / n;
        }
        List<Double> band = new ArrayList<>();
        double total = 0;
        for (int k = 0; k < bins; k++) if (k * rate / n >= cfg.toolBandLowHz) { band.add(spec[k]); total += spec[k]; }
        int kTop = Math.max(3, Filters.roundHalfEven(0.1 * band.size()));
        double conc = 0;
        if (total > 0 && !band.isEmpty()) {
            band.sort(null);
            double top = 0;
            for (int i = Math.max(0, band.size() - kTop); i < band.size(); i++) top += band.get(i);
            conc = top / total;
        }
        return new double[]{Math.sqrt(tool), Math.sqrt(motion), dom, conc};
    }

    // ---- session ----

    void resetSession() {
        state = "no_tool";
        sessionToolS = 0; sessionObservedS = 0; bouts = 0;
        boutS = 0; longestBoutS = 0; candS = 0; unobservedS = 0;
        hist.clear(); doms.clear();
        lastT = null;
    }

    void onBootChange() {
        lastT = null;
        if (calState.startsWith("capturing")) calFail("interrupted_boot_change");
    }

    State update(Double tUs) {
        double dt = 0;
        if (tUs != null) {
            if (lastT != null && tUs > lastT) dt = (tUs - lastT) / 1e6;
            lastT = tUs;
        }
        double[] f = rate != null ? features() : null;
        if (dt > cfg.maxStepS) { unobservedS += dt; dt = 0; }
        if (calState.startsWith("capturing")) calObserve(f, dt);
        if (f == null) {
            unobservedS += dt;
            if (state.equals("tool") && unobservedS >= cfg.offHoldS) endBout();
            candS = 0;
            return record("unknown", null, false, null);
        }
        unobservedS = 0;
        sessionObservedS += dt;
        boolean ratioOK = f[0] >= cfg.toolToMotionMin * f[1] && f[3] >= cfg.concentrationMin;
        boolean candidate = f[0] >= cfg.onThreshold && ratioOK;
        boolean active = f[0] >= cfg.offThreshold && ratioOK;
        Double steady = null;
        if (state.equals("no_tool")) {
            candS = candidate ? candS + dt : 0;
            if (candidate) {
                doms.addLast(new double[]{dt, f[2]});
                while (!doms.isEmpty() && sumDt(doms) - doms.peekFirst()[0] >= cfg.onHoldS) doms.removeFirst();
            } else {
                doms.clear();
            }
            steady = steadiness(doms, cfg);
            if (candidate && candS >= cfg.onHoldS && (steady == null ? 0 : steady) >= cfg.steadyMin) {
                state = "tool";
                bouts++;
                boutS = candS;
                sessionToolS += candS;
                hist.clear();
            }
        } else {
            boutS += dt;
            if (active) sessionToolS += dt;
            hist.addLast(new double[]{dt, active ? 1 : 0});
            double span = sumDt(hist);
            while (!hist.isEmpty() && span - hist.peekFirst()[0] >= cfg.offHoldS) span -= hist.removeFirst()[0];
            double on = 0;
            for (double[] h : hist) if (h[1] > 0) on += h[0];
            if (span >= cfg.offHoldS && on < 0.3 * span) endBout();
        }
        longestBoutS = Math.max(longestBoutS, state.equals("tool") ? boutS : 0);
        return record(state, f, candidate, steady);
    }

    private static double sumDt(ArrayDeque<double[]> q) { double s = 0; for (double[] e : q) s += e[0]; return s; }

    private static Double steadiness(Iterable<double[]> q, Config cfg) {
        List<Double> hz = new ArrayList<>();
        for (double[] e : q) if (!Double.isNaN(e[1])) hz.add(e[1]);
        if (hz.isEmpty()) return null;
        double[] a = new double[hz.size()];
        for (int i = 0; i < a.length; i++) a[i] = hz.get(i);
        double med = Filters.median(a), tol = Math.max(cfg.steadyTolHz, 0.1 * med);
        int ok = 0;
        for (double h : a) if (Math.abs(h - med) <= tol) ok++;
        return (double) ok / a.length;
    }

    private void endBout() {
        state = "no_tool";
        longestBoutS = Math.max(longestBoutS, boutS);
        boutS = 0;
        hist.clear(); doms.clear();
        candS = 0;
    }

    private State record(String st, double[] f, boolean candidate, Double steady) {
        State s = new State();
        if (state.equals("tool") && boutS >= cfg.continuousWarnS) s.warnings.add("continuous_tool_use_long");
        if (sessionToolS >= cfg.sessionWarnS) s.warnings.add("session_tool_time_high");
        else if (sessionToolS >= cfg.sessionNoticeFraction * cfg.sessionWarnS) s.warnings.add("session_tool_time_approaching");
        s.state = st;
        s.candidate = candidate;
        if (f != null) {
            s.toolBand = Filters.round(f[0], 3); s.motionBand = Filters.round(f[1], 3);
            s.dominantHz = Double.isNaN(f[2]) ? null : Filters.round(f[2], 1);
            s.concentration = Filters.round(f[3], 3);
        }
        s.steadiness = steady == null ? null : Filters.round(steady, 3);
        s.boutS = state.equals("tool") ? boutS : 0;
        s.bouts = bouts;
        s.longestBoutS = Math.max(longestBoutS, state.equals("tool") ? boutS : 0);
        s.sessionToolS = sessionToolS;
        s.sessionObservedS = sessionObservedS;
        s.calibrationState = calState;
        s.calibrationSource = calSource;
        s.captureLeftS = captureLeftS();
        return s;
    }

    // ---- tool calibration (still -> movement -> tool) ----

    Double captureLeftS() {
        if (!calState.startsWith("capturing") || calPhase == null) return null;
        return Math.max(0, duration(calPhase) - calElapsed);
    }

    double duration(String phase) {
        return phase.equals("still") ? cfg.calStillS : phase.equals("movement") ? cfg.calMovementS : cfg.calToolS;
    }

    void calibrationStart(String phase) {
        if (!Arrays.asList("still", "movement", "tool").contains(phase)) { events.add(Event.toolCalibrationRejected("unknown_phase")); return; }
        if (phase.equals("tool") && !captures.containsKey("movement")) { events.add(Event.toolCalibrationRejected("movement_capture_required_first")); return; }
        calPhase = phase;
        calElapsed = 0;
        captures.remove(phase);
        calBuf = new ArrayList<>();
        calState = "capturing_" + phase;
        events.add(Event.toolCalibration(calState, null, null));
    }

    void calibrationCancel() {
        if (!calState.startsWith("capturing")) return;
        calPhase = null;
        calState = "cancelled";
        events.add(Event.toolCalibration(calState, null, null));
    }

    void calibrationReset() {
        calPhase = null;
        cfg = defaults.clone();
        calState = "uncalibrated"; calSource = "default";
        captures.clear(); calResult = null;
        events.add(Event.toolCalibration(calState, "reset_to_defaults", null));
    }

    /** Restore a saved calibration (thresholds only). */
    void install(CalResult r) {
        Double on = r.thresholds.get("on_threshold_ms2"), off = r.thresholds.get("off_threshold_ms2"),
               ratio = r.thresholds.get("tool_to_motion_min"), conc = r.thresholds.get("concentration_min");
        if (on == null || off == null || ratio == null || conc == null || off <= 0 || off > on) return;
        cfg = defaults.clone();
        cfg.onThreshold = on; cfg.offThreshold = off; cfg.toolToMotionMin = ratio; cfg.concentrationMin = conc;
        calResult = r;
        calState = "calibrated"; calSource = "calibrated";
        events.add(Event.toolCalibration(calState, "restored", r));
    }

    private void calFail(String reason) {
        lastFailure = reason;
        calPhase = null;
        calState = "failed";
        events.add(Event.toolCalibration(calState, reason, calResult));
    }

    private void calObserve(double[] f, double dt) {
        String phase = calPhase;
        if (phase == null) return;
        if (f == null) {
            if (calElapsed > 0) calFail("interrupted_data_gap");
            return;
        }
        calElapsed += dt;
        if (calElapsed > cfg.calDiscardS) calBuf.add(new Win(dt, f[0], f[1], f[3], Double.isNaN(f[2]) ? null : f[2]));
        if (calElapsed < duration(phase)) return;
        captures.put(phase, calBuf);
        calPhase = null;
        if (calBuf.size() < 10) { calResult = null; calFail("insufficient_data"); return; }
        if (phase.equals("tool")) solve();
        else {
            calState = phase + "_captured";
            events.add(Event.toolCalibration(calState, null, null));
        }
    }

    private static double[] col(List<Win> w, int which) {
        double[] a = new double[w.size()];
        for (int i = 0; i < a.length; i++) {
            Win x = w.get(i);
            a[i] = which == 0 ? x.tool : which == 1 ? x.motion : which == 2 ? x.conc : x.tool / Math.max(x.motion, 1e-6);
        }
        return a;
    }

    private static double[] q(double[] v) {
        return new double[]{Filters.round(Filters.percentile(v, 50), 3), Filters.round(Filters.percentile(v, 95), 3)};
    }

    private static Summary summary(List<Win> rows) {
        Summary s = new Summary();
        s.windows = rows.size();
        s.toolBand = q(col(rows, 0)); s.motionBand = q(col(rows, 1));
        s.ratio = q(col(rows, 3)); s.concentration = q(col(rows, 2));
        return s;
    }

    private void solve() {
        List<Win> mv = captures.get("movement"), tl = captures.get("tool"), st = captures.get("still");
        CalResult res = new CalResult();
        for (Map.Entry<String, List<Win>> e : captures.entrySet()) res.summaries.put(e.getKey(), summary(e.getValue()));
        double floor = st == null ? 0.3 : Math.max(0.3, 3 * Filters.percentile(col(st, 0), 90));
        if (Filters.median(col(tl, 0)) < Math.max(floor, 0.5)) { calResult = res; calFail("no_tool_vibration_detected"); return; }
        if (st != null && Filters.median(col(mv, 1)) < Math.max(0.3, 3 * Filters.median(col(st, 1)))) {
            calResult = res; calFail("no_movement_detected"); return;
        }
        String[] names = {"tool_band", "ratio", "concentration"};
        int[] cols = {0, 3, 2};
        Map<String, Double> thr = new HashMap<>();
        for (int i = 0; i < 3; i++) {
            double[] m = col(mv, cols[i]), t = col(tl, cols[i]);
            double mHi = Filters.percentile(m, 95), tLo = Filters.percentile(t, 15);
            boolean sep = tLo > mHi;
            res.separates.put(names[i], sep);
            if (sep) thr.put(names[i], names[i].equals("concentration") ? (mHi + tLo) / 2 : Math.sqrt(mHi * tLo));
            else thr.put(names[i], 0.8 * Filters.percentile(t, 10));
        }
        double on = Math.max(thr.get("tool_band"), floor);
        Config nw = cfg.clone();
        nw.onThreshold = Filters.round(on, 3);
        nw.offThreshold = Filters.round(Math.max(Math.min(0.6 * on, on), Math.min(floor, on)), 3);
        nw.toolToMotionMin = Filters.round(thr.get("ratio"), 3);
        nw.concentrationMin = Filters.round(Math.min(Math.max(thr.get("concentration"), 0), 1), 3);
        boolean[] mp = new boolean[mv.size()];
        int mpN = 0, tpN = 0;
        for (int i = 0; i < mv.size(); i++) { mp[i] = passes(mv.get(i), nw); if (mp[i]) mpN++; }
        for (Win w : tl) if (passes(w, nw)) tpN++;
        // replay the movement capture through on-hold + steadiness: would any of it start a bout?
        int falseBouts = 0;
        double run = 0;
        ArrayDeque<double[]> dq = new ArrayDeque<>();
        for (int i = 0; i < mv.size(); i++) {
            Win w = mv.get(i);
            if (!mp[i]) { run = 0; dq.clear(); continue; }
            run += w.dt;
            dq.addLast(new double[]{w.dt, w.dom == null ? Double.NaN : w.dom});
            while (!dq.isEmpty() && sumDt(dq) - dq.peekFirst()[0] >= cfg.onHoldS) dq.removeFirst();
            Double steady = steadiness(dq, cfg);
            if (run >= cfg.onHoldS && (steady == null ? 0 : steady) >= cfg.steadyMin) { falseBouts++; run = 0; dq.clear(); }
        }
        res.toolWindowsAccepted = Filters.round((double) tpN / tl.size(), 3);
        res.movementWindowsAccepted = Filters.round((double) mpN / mv.size(), 3);
        res.movementFalseBouts = falseBouts;
        res.thresholds.put("on_threshold_ms2", nw.onThreshold);
        res.thresholds.put("off_threshold_ms2", nw.offThreshold);
        res.thresholds.put("tool_to_motion_min", nw.toolToMotionMin);
        res.thresholds.put("concentration_min", nw.concentrationMin);
        calResult = res;
        if (res.toolWindowsAccepted < cfg.calToolPassMin) { calFail("tool_not_reliably_detected"); return; }
        if (res.movementWindowsAccepted > cfg.calMovementPassMax || falseBouts > 0) { calFail("movement_looks_like_tool"); return; }
        cfg = nw;
        calState = "calibrated"; calSource = "calibrated";
        events.add(Event.toolCalibration(calState, null, res));
    }

    private static boolean passes(Win w, Config c) {
        return w.tool >= c.onThreshold && w.tool >= c.toolToMotionMin * w.motion && w.conc >= c.concentrationMin;
    }
}
