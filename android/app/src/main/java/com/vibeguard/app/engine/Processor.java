package com.vibeguard.app.engine;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The on-phone processor: contract records in, a snapshot and events out. Port of processing/processor.py in
 * host-clock mode (a stream is stale after 1 s without data, judged against the phone's clock). One per sleeve.
 * Not thread-safe: call it from one thread.
 */
public final class Processor {

    /** What the screens read, after every record. */
    public static final class Snapshot {
        public String deviceId, bootId, source, firmware;
        public Double envelope, activation, vibration, scoreRate, calibrationLeftS;
        public String envelopeStatus = "unavailable", activationStatus = "unavailable";
        public String calibrationState = "uncalibrated", calibrationReason, scorePausedReason = "no_data";
        public double calibrationDurationS = 3, scoreTotal, scoredS;
        public boolean vibrationClipped;
        public String vibrationStatus = "unavailable";
        public ToolUse.State tool = new ToolUse.State();
        public Set<String> staleStreams = new HashSet<>();
        public long records, seqGaps, missingRecords;
        public Exposure.Snapshot exposure = new Exposure.Snapshot();
    }

    /**
     * EXPERIMENTAL exposure score (processing/score.py formula): rate [au/h] = (V / 1 m/s^2)^2 * (1 + clamp(a, 0, 2)).
     * Interval bookkeeping as the Swift engine: each vibration update covers the time since the previous one (at
     * most 0.25 s) and counts only while the latest activation is usable and no older than 0.25 s.
     */
    static final class Score {
        double k = 1, vRef = 1, exponent = 2, weight = 1, cap = 2, freshS = 0.25;
        double cumulative, validS, excludedS;
        Double rate;
        String pausedReason = "no_data";
        private Double lastVibT, actT, actValue;
        private boolean act;

        double rateFor(double v, Double a) {
            return k * Math.pow(v / vRef, exponent) * (1 + weight * Math.min(cap, Math.max(0, a == null ? 0 : a)));
        }

        void updateActivation(Double t, Emg.Normalized n) {
            if (t == null) return;
            act = true; actT = t;
            actValue = n.usable() ? n.value : null;
        }

        void updateVibration(Double t, Vibration.State v) {
            if (t == null) return;
            Double vib = v.usable() ? v.combinedRms : null;
            String why = null;
            if (vib == null) why = "vibration_" + (v.reasons.isEmpty() ? v.status : v.reasons.get(v.reasons.size() - 1));
            else if (!act) why = "activation_no_data";
            else if (actValue == null) why = "activation_uncalibrated_or_unavailable";
            else if (t - actT > freshS * 1e6) why = "activation_stale";
            pausedReason = why;
            rate = why == null ? rateFor(vib, actValue) : null;
            if (lastVibT != null && t > lastVibT) {
                double dt = Math.min(t - lastVibT, freshS * 1e6) / 1e6;
                if (rate != null) { cumulative += rate * dt / 3600; validS += dt; } else excludedS += dt;
            }
            lastVibT = t;
        }

        void onBootChange() { lastVibT = null; act = false; actT = null; actValue = null; rate = null; pausedReason = "boot_change"; }
        void resetAccumulation() { cumulative = 0; validS = 0; excludedS = 0; }
    }

    private static final class Stream {
        final String name;
        Long nextIndex;
        Double lastT, lastRx, rate;
        String[] channels;
        boolean stale, rateMismatch;
        String deviceState = "unknown";
        Stream(String n) { name = n; }
        void fullReset() { nextIndex = null; lastT = null; lastRx = null; rate = null; channels = null; stale = false; rateMismatch = false; deviceState = "unknown"; }
    }

    public double staleAfterS = 1.0;
    public final double countsPerG;
    public final Snapshot snapshot = new Snapshot();
    private final List<Event> events = new ArrayList<>();

    final Emg.Envelope emg = new Emg.Envelope();
    final Emg.Calibrator grip = new Emg.Calibrator();
    final Vibration vib;
    final ToolUse tool;
    final Score score = new Score();
    final Exposure exposure = new Exposure();

    private final Map<String, Stream> streams = new LinkedHashMap<>();
    private Map<String, Records.StreamDecl> declared = new HashMap<>();
    private Long seqLast;
    private final Set<Long> recent = new HashSet<>();
    private final ArrayDeque<Long> recentOrder = new ArrayDeque<>();

    public Processor(double countsPerG) {
        this.countsPerG = countsPerG;
        vib = new Vibration(countsPerG);
        tool = new ToolUse(new ToolUse.Config(), countsPerG);
        emg.onReset = grip::onSignalReset;
        streams.put("emg", new Stream("emg"));
        streams.put("imu", new Stream("imu"));
    }

    // ---- API ----

    public boolean feedLine(String line, double rx) {
        Records.Record r = Records.parse(line);
        if (r == null) return false;
        feed(r, rx);
        return true;
    }

    public void feed(Records.Record r, double rx) {
        if (snapshot.deviceId == null) snapshot.deviceId = r.deviceId;
        if (!r.deviceId.equals(snapshot.deviceId)) return;               // one processor per sleeve
        if (!r.bootId.equals(snapshot.bootId)) {
            String previous = snapshot.bootId;
            snapshot.bootId = r.bootId;
            seqLast = null; recent.clear(); recentOrder.clear(); declared = new HashMap<>();
            for (Stream s : streams.values()) s.fullReset();
            emg.reset("no_data"); vib.reset("no_data"); tool.reset("no_data");
            events.add(Event.bootChange(previous));
            if (previous != null) { grip.onBootChange(); score.onBootChange(); tool.onBootChange(); exposure.onBootChange(); }
        }
        if (recent.contains(r.seq)) return;
        if (seqLast != null && r.seq < seqLast) return;
        if (seqLast != null && r.seq > seqLast + 1) {
            snapshot.seqGaps++;
            snapshot.missingRecords += r.seq - seqLast - 1;
            events.add(Event.seqGap(r.seq - seqLast - 1));
        }
        if (snapshot.source != null && !snapshot.source.equals(r.source)) {
            grip.invalidate("provenance_changed");
            score.resetAccumulation();                                    // never sum live and simulated data
            exposure.resetAccumulation();
            for (Stream s : streams.values()) resetStream(s, "provenance_changed");
        }
        snapshot.source = r.source;
        recent.add(r.seq); recentOrder.addLast(r.seq);
        if (recentOrder.size() > 256) recent.remove(recentOrder.removeFirst());
        seqLast = seqLast == null ? r.seq : Math.max(seqLast, r.seq);
        snapshot.records++;

        switch (r.type) {
            case "hello":
                declared = r.streams;
                snapshot.firmware = r.firmware;
                break;
            case "status":
                for (Map.Entry<String, String> e : r.sensors.entrySet()) {
                    Stream st = streams.get(e.getKey());
                    if (st == null) continue;
                    String prev = st.deviceState, state = e.getValue();
                    st.deviceState = state;
                    if (state.equals(prev) || (prev.equals("unknown") && state.equals("ok"))) continue;
                    events.add(Event.sensor(st.name, state));
                    if (state.equals("disconnected") || state.equals("stale")) {
                        resetStream(st, "device_reports_" + state);
                        features(st, st.lastT);
                    }
                }
                break;
            default:
                onBatch(r.batch, rx);
        }
        checkStale(rx);
        collect();
    }

    /** Call about every 50 ms so silence is reported as stale. */
    public void poll(double now) { checkStale(now); collect(); }

    public List<Event> takeEvents() {
        List<Event> e = new ArrayList<>(events);
        events.clear();
        return e;
    }

    public void gripCalibration(String phase) {
        if (phase.equals("cancel")) grip.cancel(); else grip.start(phase, emg.rate == null ? null : snapshot.bootId);
        collect();
    }

    public void toolCalibration(String phase) {
        switch (phase) {
            case "cancel": tool.calibrationCancel(); break;
            case "reset": tool.calibrationReset(); break;
            default: tool.calibrationStart(phase);
        }
        collect();
    }

    public void resetToolSession() { tool.resetSession(); }
    /** Lower one worker's daily limit, in HSE points (at most 400, the limit value). */
    public void setPersonalLimit(double points) {
        if (!(points > 0 && points <= Exposure.ELV_POINTS)) throw new IllegalArgumentException("limit must be in (0, 400]");
        exposure.personalLimitPoints = points;
    }

    /**
     * exposure.py grip_gate: true (its default) counts a second only while the wearer is not known to be relaxed
     * (grip under 10 % of the calibrated squeeze); false counts every second the tool detector says "tool".
     */
    public void setGripGate(boolean on) { exposure.gripGate = on; }

    /** A new day (or shift): the daily exposure starts again from zero. */
    public void resetExposure() { exposure.resetAccumulation(); snapshot.exposure = exposure.snapshot(null); }
    public void resetScore() { score.resetAccumulation(); snapshot.scoreTotal = 0; snapshot.scoredS = 0; }
    public Emg.GripCalibration gripCalibrationActive() { return grip.active; }
    public void installGrip(Emg.GripCalibration c) { grip.install(c); collect(); }
    public void installTool(ToolUse.CalResult r) { tool.install(r); collect(); }
    public String toolCalState() { return tool.calState; }
    public String toolLastFailure() { return tool.lastFailure; }
    public ToolUse.CalResult toolCalResult() { return tool.calResult; }
    public double toolCaptureDuration(String phase) { return tool.duration(phase); }

    // ---- internals ----

    private void resetStream(Stream s, String reason) {
        if (s.name.equals("emg")) emg.reset(reason); else { vib.reset(reason); tool.reset(reason); }
    }

    private void onBatch(Records.Batch b, double rx) {
        Stream st = streams.get(b.stream);
        if (st == null) return;
        Records.StreamDecl d = declared.get(b.stream);
        double rate = d != null && d.nominalRateHz != null ? d.nominalRateHz : b.sampleRateHz;
        if (st.rate == null || st.rate != rate) {
            if (st.nextIndex != null) resetStream(st, "rate_changed");
            st.rate = rate;
        }
        double tol = 0.10 * rate;
        st.rateMismatch = Math.abs(b.sampleRateHz - rate) > tol || (b.timingHz != null && Math.abs(b.timingHz - rate) > tol);
        if (st.channels != null && !java.util.Arrays.equals(st.channels, b.channels)) resetStream(st, "channels_changed");
        st.channels = b.channels;
        if (b.stream.equals("emg")) emg.configure(rate); else { vib.configure(rate); tool.configure(rate); }

        int n = b.samples.length;
        if (st.nextIndex != null) {
            if (b.firstSampleIndex < st.nextIndex) return;                // overlap: ignore
            if (b.firstSampleIndex > st.nextIndex) {
                events.add(Event.sampleGap(b.stream, b.firstSampleIndex - st.nextIndex));
                resetStream(st, "gap");
            } else if (!st.rateMismatch && st.lastT != null) {
                double dt = 1e6 / rate, step = b.times[0] - st.lastT;
                if (Math.abs(step - dt) > 0.5 * dt) resetStream(st, "timing_discontinuity");
            }
        }
        st.nextIndex = b.firstSampleIndex + n;
        st.lastT = b.times[n - 1];
        st.lastRx = rx;
        if (st.stale) { st.stale = false; events.add(Event.recovered(b.stream)); }
        Double tEnd = b.times[n - 1];
        String blocked = st.rateMismatch ? "rate_mismatch"
                : (st.deviceState.equals("disconnected") || st.deviceState.equals("stale")) ? "device_reports_" + st.deviceState
                : b.hasFlag("sensor_disconnected") ? "sensor_disconnected"
                : b.hasFlag("stale") ? "sensor_stale" : null;
        if (blocked != null) { resetStream(st, blocked); features(st, tEnd); return; }
        double[] limits = d == null ? null : d.rawLimits;
        boolean deviceClipped = b.hasFlag("clipped");
        boolean[] clipped = new boolean[n];
        for (int i = 0; i < n; i++) {
            boolean c = deviceClipped;
            if (!c && limits != null) for (Double v : b.samples[i]) if (v != null && (v <= limits[0] || v >= limits[1])) { c = true; break; }
            clipped[i] = c;
        }
        if (b.stream.equals("emg")) {
            int ci = Math.max(0, b.indexOf("emg0"));
            Double[] values = new Double[n];
            for (int i = 0; i < n; i++) values[i] = b.samples[i][ci];
            emg.process(values, clipped);
        } else {
            int ix = b.indexOf("ax"), iy = b.indexOf("ay"), iz = b.indexOf("az");
            if (ix < 0 || iy < 0 || iz < 0) return;
            double[][] rows = new double[n][];
            for (int i = 0; i < n; i++) {
                Double x = b.samples[i][ix], y = b.samples[i][iy], z = b.samples[i][iz];
                rows[i] = x == null || y == null || z == null ? null : new double[]{x, y, z};
            }
            vib.process(rows, clipped);
            tool.process(rows);
        }
        features(st, tEnd);
    }

    private void features(Stream st, Double tEnd) {
        if (st.name.equals("emg")) {
            Emg.EnvelopeState env = emg.state();
            grip.observe(env, tEnd, snapshot.bootId);
            Emg.Normalized nrm = grip.normalized(env);
            score.updateActivation(tEnd, nrm);
            exposure.updateActivation(tEnd, nrm);
            snapshot.envelope = env.value;
            snapshot.envelopeStatus = st.stale ? "unavailable" : env.status;
            snapshot.activation = nrm.value;
            snapshot.activationStatus = nrm.status;
        } else {
            Vibration.State v = vib.state();
            score.updateVibration(tEnd, v);
            snapshot.vibration = v.combinedRms;
            snapshot.vibrationStatus = v.status;
            snapshot.vibrationClipped = v.reasons.contains("clipped");
            snapshot.tool = tool.update(tEnd);
            exposure.updateVibration(tEnd, v);
            exposure.updateTool(tEnd, snapshot.tool);
            snapshot.scoreRate = score.rate;
            snapshot.scoreTotal = score.cumulative;
            snapshot.scorePausedReason = score.pausedReason;
            snapshot.scoredS = score.validS;
        }
        snapshot.exposure = exposure.snapshot(tEnd);
        refreshCalibration();
    }

    private void refreshCalibration() {
        snapshot.calibrationState = grip.state;
        snapshot.calibrationReason = grip.reason;
        snapshot.calibrationLeftS = grip.captureLeftS();
        snapshot.calibrationDurationS = grip.captureDurationS();
    }

    private void checkStale(double now) {
        for (Stream st : streams.values()) {
            if (st.stale || st.lastRx == null || now - st.lastRx <= staleAfterS) continue;
            st.stale = true;
            events.add(Event.stale(st.name));
            resetStream(st, "stale");
            features(st, st.lastT);
        }
        snapshot.staleStreams = new HashSet<>();
        for (Stream st : streams.values()) if (st.stale) snapshot.staleStreams.add(st.name);
    }

    private void collect() {
        events.addAll(grip.events); grip.events.clear();
        events.addAll(tool.events); tool.events.clear();
        refreshCalibration();
        snapshot.tool.calibrationState = tool.calState;
        snapshot.tool.calibrationSource = tool.calSource;
        snapshot.tool.captureLeftS = tool.captureLeftS();
    }
}
