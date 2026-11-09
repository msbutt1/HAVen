package com.haven.app.engine;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/**
 * Accelerometer vibration: per-axis 5 Hz high-pass (removes gravity and slow motion), RMS over 1 s, combined
 * vector magnitude in m/s^2. Port of processing/vibration.py. Unweighted, not ISO 5349.
 */
public final class Vibration {
    public static final class State {
        public final String status;
        public final List<String> reasons;
        public final Double combinedRms;
        State(String s, List<String> r, Double v) { status = s; reasons = r; combinedRms = v; }
        boolean usable() { return status.equals("ok") || status.equals("degraded"); }
    }

    static final double HIGHPASS = 5, WINDOW = 1.0, SETTLE = 0.25;
    final double scale;
    Double rate;
    private Filters.SOS filter;
    private double[] x0;
    private long since;
    private int nWin = 1, nSettle;
    private final ArrayDeque<double[]> sq = new ArrayDeque<>();   // squared filtered value per axis + clipped flag
    String resetReason = "no_data";

    Vibration(double countsPerG) { scale = 9.80665 / countsPerG; }

    void configure(double rate) {
        if (this.rate != null && this.rate == rate) return;
        this.rate = rate;
        filter = new Filters.SOS(Filters.highpass(2, HIGHPASS, rate), 3);
        nWin = Math.max(1, Filters.roundHalfEven(WINDOW * rate));
        nSettle = Filters.roundHalfEven(SETTLE * rate);
        reset("configured");
    }

    void reset(String reason) {
        x0 = null;
        if (filter != null) filter.reset();
        since = 0;
        sq.clear();
        resetReason = reason;
    }

    void process(double[][] rows, boolean[] clipped) {
        if (filter == null) return;
        for (int i = 0; i < rows.length; i++) {
            double[] r = rows[i];
            if (r == null) { reset("missing_values"); continue; }
            if (x0 == null) x0 = r.clone();
            since++;
            double[] e = new double[4];
            for (int a = 0; a < 3; a++) { double y = filter.step(r[a] - x0[a], a); e[a] = y * y; }
            e[3] = clipped[i] ? 1 : 0;
            if (since > nSettle) {
                sq.addLast(e);
                while (sq.size() > nWin) sq.removeFirst();
            }
        }
    }

    State state() {
        if (rate == null || since == 0)
            return new State("unavailable", Filters.list(resetReason.equals("configured") ? "no_data" : resetReason), null);
        if (sq.size() < nWin) {
            List<String> r = new ArrayList<>(Filters.list("warming_up"));
            if (!resetReason.equals("no_data") && !resetReason.equals("configured")) r.add(resetReason);
            return new State("warming_up", r, null);
        }
        double[] sum = new double[3];
        boolean clip = false;
        for (double[] e : sq) { for (int a = 0; a < 3; a++) sum[a] += e[a]; clip |= e[3] > 0; }
        double total = 0;
        for (int a = 0; a < 3; a++) {
            double rms = Math.sqrt(Math.max(0, sum[a]) / sq.size());
            total += (scale * rms) * (scale * rms);
        }
        return new State(clip ? "degraded" : "ok", clip ? Filters.list("clipped") : Filters.list(), Filters.round(Math.sqrt(total), 4));
    }
}
