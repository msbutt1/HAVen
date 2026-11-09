package com.haven.app.engine;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * EMG envelope (offset removal -> 20-450 Hz band-pass -> 100 ms moving RMS) and the relaxed/squeeze grip
 * calibration. Port of processing/emg.py and calibration.py (via the Swift engine). A relative muscle-activity
 * proxy in ADC counts: not force, not %MVC.
 */
public final class Emg {
    private Emg() {}

    public static final class EnvelopeState {
        public final String status;             // ok | degraded | warming_up | unavailable
        public final List<String> reasons;
        public final Double value;
        final long samplesTotal, clippedTotal;
        EnvelopeState(String status, List<String> reasons, Double value, long s, long c) {
            this.status = status; this.reasons = reasons; this.value = value; samplesTotal = s; clippedTotal = c;
        }
        boolean usable() { return status.equals("ok") || status.equals("degraded"); }
    }

    public interface ResetListener { void onReset(String reason); }

    public static final class Envelope {
        static final double LOW = 20, HIGH = 450, TAU = 1.0, WINDOW = 0.1, SETTLE = 0.1;
        Double rate;
        private Filters.SOS filter;
        private double alpha;
        private int nEnv = 1, nSettle = 0;
        private Double ema;
        private long since;
        private final ArrayDeque<double[]> tail = new ArrayDeque<>();   // {value, clipped}
        private int tailClipped;
        String resetReason = "no_data";
        private String configError;
        long samplesTotal, clippedTotal;
        ResetListener onReset;
        /** Share of recent samples at the input's limits (0.5 s time constant); setup waits until it is low. */
        public double saturation;
        private double satAlpha = 0.998;

        void configure(double rate) {
            if (this.rate != null && this.rate == rate) return;
            this.rate = rate;
            configError = HIGH > 0.9 * rate / 2 ? "band-pass upper edge above 0.9 x Nyquist" : null;
            filter = configError == null ? new Filters.SOS(Filters.bandpass(2, LOW, HIGH, rate), 1) : null;
            nEnv = Math.max(1, Filters.roundHalfEven(WINDOW * rate));
            nSettle = Filters.roundHalfEven(SETTLE * rate);
            alpha = Math.exp(-1 / (rate * TAU));
            satAlpha = Math.exp(-1 / (rate * 0.5));
            reset("configured");
        }

        void reset(String reason) {
            ema = null;
            if (filter != null) filter.reset();
            since = 0;
            tail.clear();
            tailClipped = 0;
            resetReason = reason;
            if (onReset != null) onReset.onReset(reason);
        }

        void process(Double[] values, boolean[] clipped) {
            if (configError != null || rate == null) return;
            for (int i = 0; i < values.length; i++) {
                Double v = values[i];
                if (v == null) { reset("missing_values"); continue; }
                double x = v;
                samplesTotal++;
                if (clipped[i]) clippedTotal++;
                saturation = satAlpha * saturation + (1 - satAlpha) * (clipped[i] ? 1 : 0);
                double m = ema == null ? x : alpha * ema + (1 - alpha) * x;
                ema = m;
                double y = filter.step(x - m, 0);
                since++;
                if (since > nSettle) {
                    tail.addLast(new double[]{y, clipped[i] ? 1 : 0});
                    if (clipped[i]) tailClipped++;
                    while (tail.size() > nEnv) if (tail.removeFirst()[1] > 0) tailClipped--;
                }
            }
        }

        EnvelopeState state() {
            if (configError != null) return new EnvelopeState("unavailable", Filters.list("filter_config_invalid"), null, samplesTotal, clippedTotal);
            if (rate == null || (since == 0 && (resetReason.equals("no_data") || resetReason.equals("configured"))))
                return new EnvelopeState("unavailable", Filters.list("no_data"), null, samplesTotal, clippedTotal);
            if (tail.size() < nEnv) {
                if (since == 0) return new EnvelopeState("unavailable", Filters.list(resetReason), null, samplesTotal, clippedTotal);
                List<String> r = new ArrayList<>(Filters.list("warming_up"));
                if (!resetReason.equals("no_data") && !resetReason.equals("configured")) r.add(resetReason);
                return new EnvelopeState("warming_up", r, null, samplesTotal, clippedTotal);
            }
            double sum = 0;
            for (double[] t : tail) sum += t[0] * t[0];
            double rms = Math.sqrt(sum / tail.size());
            boolean clip = tailClipped > 0;
            return new EnvelopeState(clip ? "degraded" : "ok", clip ? Filters.list("clipped") : Filters.list(),
                    Filters.round(rms, 4), samplesTotal, clippedTotal);
        }
    }

    public static final class GripCalibration {
        public String deviceId = "", bootId;
        public double relaxed, squeeze;
        public long createdAtMs;
    }

    public static final class Normalized {
        public final String status;
        public final List<String> reasons;
        public final Double value;               // unbounded: 0 relaxed, 1 calibrated squeeze
        Normalized(String s, List<String> r, Double v) { status = s; reasons = r; value = v; }
        boolean usable() { return status.equals("ok") || status.equals("degraded"); }
    }

    /** Relaxed/squeeze calibration state machine (calibration.py). Live allowance: 10 % clipped samples. */
    public static final class Calibrator {
        double relaxedS = 3, squeezeS = 3, discardS = 0.5;
        int minUpdates = 10;
        double maxClippedFraction = 0.10, minRatio = 2.0, minSeparation = 1.0;
        static final double MAX_PAUSE_S = 1.0;
        static final java.util.Set<String> PAUSABLE = new java.util.HashSet<>(java.util.Arrays.asList(
                "gap", "timing_discontinuity", "missing_values"));

        String state = "uncalibrated", reason;
        GripCalibration active;
        private Double relaxedRef;
        private String relaxedBoot;
        private String capPhase, capBoot;
        private Double tFirst, lastT;
        private long samples0, clipped0;
        private final List<Double> values = new ArrayList<>();
        final List<Event> events = new ArrayList<>();

        String capturing() { return capPhase; }
        double captureDurationS() { return "squeeze".equals(capPhase) ? squeezeS : relaxedS; }
        Double captureLeftS() {
            if (capPhase == null) return null;
            double dur = captureDurationS();
            if (tFirst == null || lastT == null) return dur;
            return Math.max(0, dur - (lastT - tFirst) / 1e6);
        }

        void start(String phase, String boot) {
            if (boot == null) { reject(phase, "emg_not_configured"); return; }
            if (phase.equals("squeeze")) {
                if (!state.equals("relaxed_captured") || relaxedRef == null) { reject(phase, "relaxed_reference_missing"); return; }
                if (!boot.equals(relaxedBoot)) { reject(phase, "relaxed_reference_incompatible"); return; }
            } else {
                active = null;
                relaxedRef = null;
            }
            capPhase = phase; capBoot = boot; tFirst = null; lastT = null; values.clear();
            set("capturing_" + phase, null, null);
        }

        void cancel() { if (capPhase != null) fail("cancelled", null); }

        void install(GripCalibration c) {
            active = c; capPhase = null; relaxedRef = null;
            set("calibrated", "restored", null);
        }

        void onSignalReset(String why) {
            if (why.equals("no_data") || why.equals("configured")) return;
            // a short Wi-Fi gap or timing break re-warms the envelope (~0.2 s): the capture pauses (observe), it
            // does not fail; it fails only if the signal stays unusable for longer than MAX_PAUSE_S
            if (capPhase != null && tFirst != null && !PAUSABLE.contains(why)) fail("interrupted_" + why, null);
        }

        void onBootChange() {
            if (capPhase != null) fail("interrupted_boot_change", null);
            invalidate("boot_change");
        }

        void invalidate(String why) {
            if (capPhase != null) fail("interrupted_" + why, null);
            if (active != null) {
                active = null; relaxedRef = null;
                set("invalidated", why, null);
            } else if (relaxedRef != null) {
                relaxedRef = null;
                if (state.equals("relaxed_captured")) set("invalidated", why, null);
            }
        }

        void observe(EnvelopeState env, Double t, String boot) {
            if (capPhase == null) return;
            if (boot == null || !boot.equals(capBoot)) { fail("interrupted_processing_change", null); return; }
            if (!env.usable() || t == null || env.value == null) {
                if (tFirst != null && t != null && lastT != null && (t - lastT) / 1e6 > MAX_PAUSE_S)
                    fail("interrupted_" + (env.reasons.isEmpty() ? env.status : env.reasons.get(env.reasons.size() - 1)), null);
                return;
            }
            if (lastT != null && t <= lastT) return;
            if (tFirst == null) { tFirst = t; samples0 = env.samplesTotal; clipped0 = env.clippedTotal; }
            lastT = t;
            double elapsed = (t - tFirst) / 1e6;
            if (elapsed >= discardS) values.add(env.value);
            if (elapsed >= (capPhase.equals("relaxed") ? relaxedS : squeezeS)) finish(env);
        }

        Normalized normalized(EnvelopeState env) {
            if (active == null) {
                List<String> r = new ArrayList<>();
                r.add(!state.equals("calibrated") ? state : "uncalibrated");
                if (reason != null && (state.equals("failed") || state.equals("invalidated"))) r.add(reason);
                return new Normalized("unavailable", r, null);
            }
            if (!env.usable() || env.value == null)
                return new Normalized(env.status.equals("warming_up") ? "warming_up" : "unavailable", env.reasons, null);
            return new Normalized(env.status, env.reasons,
                    Filters.round((env.value - active.relaxed) / (active.squeeze - active.relaxed), 4));
        }

        private void finish(EnvelopeState env) {
            long samples = env.samplesTotal - samples0, clipped = env.clippedTotal - clipped0;
            double frac = samples > 0 ? (double) clipped / samples : 1;
            if (values.size() < minUpdates) { fail("insufficient_data", null); return; }
            if (frac > maxClippedFraction) { fail("clipped", Filters.map("clipped_fraction", Filters.round(frac, 6))); return; }
            double[] v = new double[values.size()];
            for (int i = 0; i < v.length; i++) v[i] = values.get(i);
            double med = Filters.round(Filters.median(v), 4);
            String phase = capPhase, boot = capBoot;
            capPhase = null;
            if (phase.equals("relaxed")) {
                relaxedRef = med; relaxedBoot = boot;
                set("relaxed_captured", null, Filters.map("relaxed", med));
                return;
            }
            double rel = relaxedRef;
            if (!(med >= minRatio * rel && med - rel >= minSeparation)) {
                relaxedRef = null;
                fail("insufficient_separation", Filters.map("relaxed", rel, "squeeze", med));
                return;
            }
            GripCalibration c = new GripCalibration();
            c.bootId = boot; c.relaxed = rel; c.squeeze = med; c.createdAtMs = System.currentTimeMillis();
            active = c;
            relaxedRef = null;
            set("calibrated", null, Filters.map("relaxed", rel, "squeeze", med));
        }

        private void fail(String why, Map<String, Double> detail) {
            capPhase = null; relaxedRef = null;
            set("failed", why, detail);
        }

        private void reject(String phase, String why) { events.add(Event.calibrationRejected(phase, why)); }

        private void set(String s, String r, Map<String, Double> detail) {
            state = s; reason = r;
            events.add(Event.calibration(s, r, detail == null ? new HashMap<>() : detail));
        }
    }
}
