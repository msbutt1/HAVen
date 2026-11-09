package com.haven.app.engine;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;

/**
 * Daily hand-arm vibration exposure ESTIMATE: ISO 5349-1 A(8), EU/UK limits, HSE points. Port of
 * processing/exposure.py (and the interval tracking it shares with score.py, ``_Track``).
 *
 * <pre>
 *   A(8)   = sqrt( sum(a_hv^2 * dt) / 8 h )
 *   points = 2 * a^2 * hours = 100 * (A(8) / 2.5)^2      action value 100 points, limit value 400 points
 * </pre>
 * A second counts only while the tool detector says "tool"; the headline ("gripped") also leaves out time the
 * wearer was KNOWN not to be gripping (calibrated activation below 0.10). Unknown grip still counts.
 * A monitoring estimate from a wrist-worn IMU (unweighted), not an ISO 5349-2 measurement.
 */
public final class Exposure {
    public static final double EAV_POINTS = 100, ELV_POINTS = 400, POINTS_PER_HOUR = 2.0;
    static final int APDF_BINS = 200;
    static final double APDF_MAX = 2.0;

    // ExposureConfig defaults
    double eav = 2.5, elv = 5.0, t0S = 8 * 3600, gripActivationMin = 0.10;
    boolean gripGate = true;
    double personalLimitPoints = 400;
    double vibFreshS = 0.25, toolFreshS = 0.25, actFreshS = 0.25;
    double yearsK = 31.8, yearsExp = -1.06, minA8ForYears = 1.0;

    /** One input update (score.py _Input, plus the tool fields of exposure.py _ToolInput). */
    static final class Input {
        final double t; final boolean usable; final Double value; final String status; final List<String> reasons;
        final boolean degraded, tool;
        Input(double t, boolean usable, Double value, String status, List<String> reasons, boolean degraded, boolean tool) {
            this.t = t; this.usable = usable; this.value = value; this.status = status; this.reasons = reasons;
            this.degraded = degraded; this.tool = tool;
        }
    }

    static String blockKey(String name, Input i) {
        return name + "_" + (i.reasons.isEmpty() ? i.status : String.join("+", i.reasons));
    }

    /** Coverage of one input's timeline by its updates (score.py _Track). */
    static final class Track {
        final String name;
        final double freshUs;
        Double firstT, lastT;
        Input latest;
        final ArrayDeque<Object[]> segs = new ArrayDeque<>();   // {t0, t1, Input or null (stale gap)}

        Track(String name, double freshS) { this.name = name; freshUs = freshS * 1e6; }

        void reset() { firstT = null; lastT = null; latest = null; segs.clear(); }

        boolean add(double t, Input inp) {
            if (lastT != null && t <= lastT) return false;
            if (lastT == null) firstT = t;
            else {
                double start = Math.max(lastT, t - freshUs);
                if (start > lastT) segs.addLast(new Object[]{lastT, start, null});
                segs.addLast(new Object[]{start, t, inp});
            }
            lastT = t; latest = inp;
            return true;
        }

        double determinedUntil(double newest) {
            if (lastT == null) return newest - freshUs;
            return Math.max(lastT, newest - freshUs);
        }

        /** {key or null, Input or null}. */
        Object[] blockAt(double b, String uncovered) {
            if (firstT == null || b <= firstT) return new Object[]{name + "_no_data", null};
            if (b > lastT) return new Object[]{name + "_" + uncovered, null};
            for (Object[] s : segs) {
                double t0 = (Double) s[0], t1 = (Double) s[1];
                if (t0 < b && b <= t1) {
                    Input inp = (Input) s[2];
                    if (inp == null) return new Object[]{name + "_stale", null};
                    return new Object[]{inp.usable ? null : blockKey(name, inp), inp};
                }
            }
            return new Object[]{name + "_no_data", null};
        }

        void prune(double cursor) { while (!segs.isEmpty() && (Double) segs.peekFirst()[1] <= cursor) segs.removeFirst(); }

        void boundaries(double a, double b, TreeSet<Double> out) {
            for (Object[] s : segs) for (int k = 0; k < 2; k++) { double p = (Double) s[k]; if (a < p && p < b) out.add(p); }
            if (firstT != null && a < firstT && firstT < b) out.add(firstT);
            if (lastT != null && a < lastT && lastT < b) out.add(lastT);
        }
    }

    /** One accumulation: energy sum(a^2 dt) and contact time. */
    public static final class Gate {
        public double e2s, contactS;
        void add(double a, double dur) { e2s += a * a * dur; contactS += dur; }
    }

    public static double a8FromEnergy(double e2s, double t0S) { return Math.sqrt(Math.max(0, e2s) / t0S); }
    public static double pointsFromEnergy(double e2s) { return POINTS_PER_HOUR * Math.max(0, e2s) / 3600.0; }

    /** Hours at aNow until points reach level (0 if already there; null if no exposure now). */
    public static Double hoursUntil(double points, double level, Double aNow) {
        if (points >= level) return 0.0;
        if (aNow == null || !(aNow > 0)) return null;
        return (level - points) / (POINTS_PER_HOUR * aNow * aNow);
    }

    final Track vib = new Track("vibration", vibFreshS), tool = new Track("tool", toolFreshS), act = new Track("activation", actFreshS);
    int accumulation;
    Gate toolOn, gripped;
    double gripUnknownS, notGrippingS, noToolS, degradedS, excludedS;
    private double[] apdf;
    private double actSum, actS;
    int bootSegments;
    private Double cursor;
    Double newest;
    private boolean finalisedAny;

    Exposure() { newAccumulation(); }

    private Track[] required() { return new Track[]{vib, tool}; }
    private Track[] all() { return new Track[]{vib, tool, act}; }

    private void newAccumulation() {
        accumulation++;
        toolOn = new Gate(); gripped = new Gate();
        gripUnknownS = 0; notGrippingS = 0; noToolS = 0; degradedS = 0; excludedS = 0;
        apdf = new double[APDF_BINS]; actSum = 0; actS = 0;
        bootSegments = 1;
        segmentReset();
    }

    private void segmentReset() {
        cursor = null; newest = null; finalisedAny = false;
        for (Track t : all()) t.reset();
    }

    void flush(String reason) { if (newest != null) finalise(newest, reason); }
    void onBootChange() { flush("boot_change"); bootSegments++; segmentReset(); }
    void resetAccumulation() { newAccumulation(); }

    void updateVibration(Double t, Vibration.State v) {
        boolean usable = v.usable() && v.combinedRms != null;
        update(vib, t, new Input(t == null ? 0 : t, usable, usable ? v.combinedRms : null, v.status, v.reasons,
                v.status.equals("degraded"), false));
    }

    void updateTool(Double t, ToolUse.State s) {
        boolean ok = !s.state.equals("unknown");               // tool record status "ok" with state tool/no_tool
        update(tool, t, new Input(t == null ? 0 : t, ok, null, ok ? "ok" : "unavailable", new ArrayList<>(), false,
                s.state.equals("tool")));
    }

    void updateActivation(Double t, Emg.Normalized n) {
        boolean usable = n.usable() && n.value != null;
        update(act, t, new Input(t == null ? 0 : t, usable, usable ? n.value : null, n.status, n.reasons,
                n.status.equals("degraded"), false));
    }

    private void update(Track track, Double t, Input inp) {
        if (t == null || !track.add(t, inp)) return;
        if (track != act && (cursor == null || (!finalisedAny && t < cursor))) cursor = t;
        if (cursor == null) return;                            // activation alone never starts the clock
        newest = newest == null ? t : Math.max(newest, t);
        double bound = Double.POSITIVE_INFINITY;
        for (Track tr : all()) bound = Math.min(bound, tr.determinedUntil(newest));
        finalise(bound, "stale");
    }

    private void finalise(double until, String uncovered) {
        if (cursor == null || until <= cursor) return;
        TreeSet<Double> pts = new TreeSet<>();
        pts.add(cursor); pts.add(until);
        for (Track tr : all()) tr.boundaries(cursor, until, pts);
        Double prev = null;
        for (double b : pts) {
            if (prev == null) { prev = b; continue; }
            double a = prev;
            prev = b;
            double dur = (b - a) / 1e6;
            Object[] bv = vib.blockAt(b, uncovered), bt = tool.blockAt(b, uncovered);
            if (bv[0] != null || bt[0] != null) { excludedS += dur; continue; }
            Input v = (Input) bv[1], tl = (Input) bt[1];
            if (!tl.tool) { noToolS += dur; continue; }
            double acc = v.value;                              // weighting "none" (the default)
            toolOn.add(acc, dur);
            if (v.degraded) degradedS += dur;
            Object[] ba = act.blockAt(b, uncovered);
            Double grip = ba[0] == null && ba[1] != null ? ((Input) ba[1]).value : null;
            if (grip == null) gripUnknownS += dur;
            else {
                apdf[Math.min(APDF_BINS - 1, Math.max(0, (int) (grip / APDF_MAX * APDF_BINS)))] += dur;
                actSum += grip * dur; actS += dur;
                if (grip < gripActivationMin) { notGrippingS += dur; continue; }
            }
            gripped.add(acc, dur);
        }
        cursor = until;
        finalisedAny = true;
        for (Track tr : all()) tr.prune(until);
    }

    /** The headline and the current state, as the screens need them. */
    public static final class Snapshot {
        public String status = "paused", level = "below_eav";
        public List<String> reasons = new ArrayList<>();
        public double points, a8, contactS, energy, toolOnPoints, toolOnContactS, gripUnknownS, personalLimit;
        public Double aHvNow, pointsPerHour, hoursToEav, hoursToLimit, gripMean;
        public Boolean toolNow, counting;
    }

    Snapshot snapshot(Double tUs) {
        Double now = tUs != null ? tUs : newest;
        Snapshot s = new Snapshot();
        for (Track tr : required()) {
            if (tr.latest == null) s.reasons.add(tr.name + "_no_data");
            else if (!tr.latest.usable) s.reasons.add(blockKey(tr.name, tr.latest));
            else if (now != null && now - tr.lastT > tr.freshUs) s.reasons.add(tr.name + "_stale");
        }
        Input al = act.latest;
        Double grip = al != null && al.usable && now != null && now - act.lastT <= act.freshUs ? al.value : null;
        boolean blocked = !s.reasons.isEmpty();
        Boolean toolNow = blocked ? null : tool.latest.tool;
        Double aHv = blocked ? null : vib.latest.value;
        boolean counting = Boolean.TRUE.equals(toolNow) && !(gripGate && grip != null && grip < gripActivationMin);
        Double aNow = counting ? aHv : null;
        Gate head = gripGate ? gripped : toolOn;
        double pts = pointsFromEnergy(head.e2s);
        s.status = blocked ? "paused" : "accumulating";
        s.points = pts;
        s.a8 = a8FromEnergy(head.e2s, t0S);
        s.contactS = head.contactS;
        s.energy = head.e2s;
        s.toolOnPoints = pointsFromEnergy(toolOn.e2s);
        s.toolOnContactS = toolOn.contactS;
        s.gripUnknownS = gripUnknownS;
        s.personalLimit = personalLimitPoints;
        s.level = pts >= personalLimitPoints ? "above_limit" : pts >= EAV_POINTS ? "above_eav" : "below_eav";
        s.aHvNow = aHv; s.toolNow = toolNow; s.counting = blocked ? null : counting;
        s.pointsPerHour = aNow == null ? null : POINTS_PER_HOUR * aNow * aNow;
        s.hoursToEav = hoursUntil(pts, EAV_POINTS, aNow);
        s.hoursToLimit = hoursUntil(pts, personalLimitPoints, aNow);
        s.gripMean = actS > 0 ? actSum / actS : null;
        return s;
    }
}
