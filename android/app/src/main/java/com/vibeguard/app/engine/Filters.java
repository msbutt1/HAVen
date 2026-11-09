package com.vibeguard.app.engine;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * On-phone signal processing for the VibeGuard sleeve: a Java port of the Swift engine
 * (ios/VibeGuard/Engine), itself a port of the Python processor (signals/.../processing).
 * Plain Java (org.json only), tested on the JVM against the Python processor's outputs on
 * real recordings (ios/EngineTests/Fixtures).
 *
 * This file: digital Butterworth design (same steps as scipy.signal.butter(..., output="sos"))
 * and a streaming cascade of biquads (as scipy.signal.sosfilt, state carried across batches).
 */
public final class Filters {
    private Filters() {}

    /** One second-order section, a0 = 1. */
    public static final class Biquad {
        double b0, b1, b2, a1, a2;
        Biquad(double b0, double b1, double b2, double a1, double a2) {
            this.b0 = b0; this.b1 = b1; this.b2 = b2; this.a1 = a1; this.a2 = a2;
        }
    }

    private static final class Cx {
        final double re, im;
        Cx(double re, double im) { this.re = re; this.im = im; }
        Cx(double re) { this(re, 0); }
        Cx add(Cx b) { return new Cx(re + b.re, im + b.im); }
        Cx sub(Cx b) { return new Cx(re - b.re, im - b.im); }
        Cx mul(Cx b) { return new Cx(re * b.re - im * b.im, re * b.im + im * b.re); }
        Cx mul(double s) { return new Cx(re * s, im * s); }
        Cx div(Cx b) {
            double d = b.re * b.re + b.im * b.im;
            return new Cx((re * b.re + im * b.im) / d, (im * b.re - re * b.im) / d);
        }
        Cx sqrt() {
            double r = Math.hypot(re, im);
            double s = Math.sqrt((r + re) / 2), t = Math.sqrt((r - re) / 2);
            return new Cx(s, im < 0 ? -t : t);
        }
    }

    public static Biquad[] lowpass(int n, double f, double fs) { return design(n, 0, f, 0, fs); }
    public static Biquad[] highpass(int n, double f, double fs) { return design(n, 1, f, 0, fs); }
    public static Biquad[] bandpass(int n, double f1, double f2, double fs) { return design(n, 2, f1, f2, fs); }

    private static double warp(double f, double fs) {
        double fs2 = 2.0;                    // scipy designs at fs = 2 after normalising to Nyquist
        return 2 * fs2 * Math.tan(Math.PI * (f / (fs / 2)) / fs2);
    }

    private static Biquad[] design(int n, int kind, double f1, double f2, double fs) {
        List<Cx> poles = new ArrayList<>();
        for (int m = -n + 1; m < n; m += 2) {
            double a = Math.PI * m / (2.0 * n);
            poles.add(new Cx(-Math.cos(a), -Math.sin(a)));
        }
        List<Cx> zeros = new ArrayList<>();
        double k = 1.0;
        if (kind == 0) {
            double wo = warp(f1, fs);
            poles.replaceAll(p -> p.mul(wo));
            k *= Math.pow(wo, n);
        } else if (kind == 1) {
            double wo = warp(f1, fs);
            Cx prodP = new Cx(1);
            for (Cx p : poles) prodP = prodP.mul(new Cx(0).sub(p));
            k *= new Cx(1).div(prodP).re;
            poles.replaceAll(p -> new Cx(wo).div(p));
            for (int i = 0; i < n; i++) zeros.add(new Cx(0));
        } else {
            double w1 = warp(f1, fs), w2 = warp(f2, fs);
            double bw = w2 - w1, wo = Math.sqrt(w1 * w2);
            List<Cx> out = new ArrayList<>();
            for (Cx p : poles) {
                Cx pl = p.mul(bw / 2);
                Cx r = pl.mul(pl).sub(new Cx(wo * wo)).sqrt();
                out.add(pl.add(r));
                out.add(pl.sub(r));
            }
            poles = out;
            for (int i = 0; i < n; i++) zeros.add(new Cx(0));
            k *= Math.pow(bw, n);
        }
        double f4 = 4.0;                     // bilinear: 2 * fs with fs = 2
        Cx num = new Cx(1), den = new Cx(1);
        for (Cx z : zeros) num = num.mul(new Cx(f4).sub(z));
        for (Cx p : poles) den = den.mul(new Cx(f4).sub(p));
        k *= num.div(den).re;
        List<Double> zz = new ArrayList<>();
        for (Cx z : zeros) zz.add(new Cx(f4).add(z).div(new Cx(f4).sub(z)).re);
        for (int i = zeros.size(); i < poles.size(); i++) zz.add(-1.0);
        List<Cx> pz = new ArrayList<>();
        for (Cx p : poles) pz.add(new Cx(f4).add(p).div(new Cx(f4).sub(p)));
        return sections(zz, pz, k);
    }

    /** Pair conjugate poles and real zeros (Butterworth zeros are all real, +1 or -1) into biquads. */
    private static Biquad[] sections(List<Double> zeros, List<Cx> poles, double gain) {
        List<Cx> cplx = new ArrayList<>();
        List<Double> real = new ArrayList<>();
        for (Cx p : poles) {
            if (p.im > 1e-12) cplx.add(p);
            else if (Math.abs(p.im) <= 1e-12) real.add(p.re);
        }
        List<Double> zr = new ArrayList<>(zeros);
        zr.sort(null);
        cplx.sort((a, b) -> Double.compare(Math.hypot(a.re, a.im), Math.hypot(b.re, b.im)));
        real.sort((a, b) -> Double.compare(Math.abs(a), Math.abs(b)));
        List<Biquad> out = new ArrayList<>();
        for (Cx p : cplx) {
            double[] b = takeZeros(zr, 2);
            out.add(new Biquad(1, b[0], b[1], -2 * p.re, p.re * p.re + p.im * p.im));
        }
        while (!real.isEmpty()) {
            double p1 = real.remove(0);
            if (!real.isEmpty()) {
                double p2 = real.remove(0);
                double[] b = takeZeros(zr, 2);
                out.add(new Biquad(1, b[0], b[1], -(p1 + p2), p1 * p2));
            } else {
                double[] b = takeZeros(zr, 1);
                out.add(new Biquad(1, b[0], b[1], -p1, 0));
            }
        }
        if (!out.isEmpty()) {
            Biquad s = out.get(0);
            s.b0 *= gain; s.b1 *= gain; s.b2 *= gain;
        }
        return out.toArray(new Biquad[0]);
    }

    private static double[] takeZeros(List<Double> zr, int count) {
        List<Double> zs = new ArrayList<>();
        for (int i = 0; i < count && !zr.isEmpty(); i++) zs.add(zr.remove(0));
        if (zs.size() == 2) return new double[]{-(zs.get(0) + zs.get(1)), zs.get(0) * zs.get(1)};
        if (zs.size() == 1) return new double[]{-zs.get(0), 0};
        return new double[]{0, 0};
    }

    /** Streaming biquad cascade over several channels, transposed direct form II, zero initial state. */
    public static final class SOS {
        private final Biquad[] s;
        private final double[][] z;

        public SOS(Biquad[] sections, int channels) {
            s = sections;
            z = new double[channels][2 * sections.length];
        }

        public void reset() { for (double[] c : z) Arrays.fill(c, 0); }

        public double step(double x, int c) {
            double v = x;
            double[] zc = z[c];
            for (int i = 0; i < s.length; i++) {
                Biquad q = s[i];
                double y = q.b0 * v + zc[2 * i];
                zc[2 * i] = q.b1 * v - q.a1 * y + zc[2 * i + 1];
                zc[2 * i + 1] = q.b2 * v - q.a2 * y;
                v = y;
            }
            return v;
        }
    }

    // ---- numpy-compatible helpers ----

    /** numpy.percentile, linear interpolation. */
    public static double percentile(double[] values, double p) {
        if (values.length == 0) return Double.NaN;
        double[] s = values.clone();
        Arrays.sort(s);
        double pos = p / 100 * (s.length - 1);
        int lo = (int) Math.floor(pos), hi = Math.min(lo + 1, s.length - 1);
        return s[lo] + (s[hi] - s[lo]) * (pos - lo);
    }

    public static double median(double[] v) { return percentile(v, 50); }

    /** Round half to even at d decimals (as the Swift engine; matches Python round on these values). */
    public static double round(double x, int d) {
        double m = Math.pow(10, d);
        return Math.rint(x * m) / m;
    }

    /** Mutable-free list/map helpers (List.of / Map.of need Android 11; minSdk is 26). */
    static java.util.List<String> list(String... s) { return java.util.Collections.unmodifiableList(Arrays.asList(s)); }
    static java.util.Map<String, Double> map(Object... kv) {
        java.util.Map<String, Double> m = new java.util.HashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], (Double) kv[i + 1]);
        return m;
    }

    /** Python round(x) for a non-negative value: half to even. */
    public static int roundHalfEven(double x) { return (int) Math.rint(x); }
}
