package com.vibeguard.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Random;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/**
 * A SIMULATED sleeve inside the app (same as the iOS DemoSleeve): the same telemetry records as the real sleeve,
 * labelled "simulated", so every screen says so. squeeze raises EMG activity; tool adds a steady 120 Hz vibration;
 * armMoving adds irregular arm movement with knocks (what must NOT count as tool use).
 */
final class DemoSleeve {
    volatile boolean squeeze, tool, armMoving;
    private final Consumer<String> out;
    private final ScheduledExecutorService ex = Executors.newSingleThreadScheduledExecutor();
    private ScheduledFuture<?> task;
    private final String boot = "b-sim-" + Long.toHexString(new Random().nextLong() & 0xffffffffL);
    private final Random rng = new Random();
    private long seq, emgIndex, imuIndex, t0;
    private int knockLeft;

    DemoSleeve(Consumer<String> out) { this.out = out; }

    synchronized void start() {
        if (task != null) return;
        t0 = System.nanoTime();
        ex.execute(() -> emit(hello()));
        task = ex.scheduleAtFixedRate(this::tick, 50, 50, TimeUnit.MILLISECONDS);
    }

    synchronized void stop() {
        if (task != null) task.cancel(false);
        task = null;
    }

    private JSONObject base(String type) throws Exception {
        JSONObject o = new JSONObject();
        o.put("schema", "vibeguard.device_telemetry"); o.put("version", 1); o.put("type", type);
        o.put("device_id", "vg-sim-demo"); o.put("boot_id", boot); o.put("seq", ++seq);
        o.put("provenance", new JSONObject().put("source", "simulated"));
        return o;
    }

    private JSONObject hello() {
        try {
            JSONObject h = base("hello");
            h.put("t_us", 0);
            h.put("firmware", new JSONObject().put("name", "in-app simulator").put("version", "demo"));
            JSONObject emg = new JSONObject().put("nominal_rate_hz", 1000.0).put("channels", new JSONArray().put("emg0"))
                    .put("units", new JSONObject().put("emg0", "adc_counts"))
                    .put("raw_limits", new JSONObject().put("min", 0).put("max", 4095));
            JSONObject imu = new JSONObject().put("nominal_rate_hz", 500.0).put("channels", new JSONArray().put("ax").put("ay").put("az"))
                    .put("units", new JSONObject().put("ax", "raw_counts").put("ay", "raw_counts").put("az", "raw_counts"))
                    .put("raw_limits", new JSONObject().put("min", -32768).put("max", 32767));
            h.put("streams", new JSONObject().put("emg", emg).put("imu", imu));
            return h;
        } catch (Exception e) {
            return null;
        }
    }

    private void tick() {
        try {
            double elapsed = (System.nanoTime() - t0) / 1e9;
            while (emgIndex / 1000.0 < elapsed - 0.05) {
                JSONObject e = base("emg_batch");
                double amp = squeeze ? 160 : 6;
                e.put("sample_rate_hz", 1000.0);
                e.put("first_sample_index", emgIndex);
                e.put("timing", new JSONObject().put("mode", "uniform").put("t0_us", emgIndex * 1000.0).put("interval_us", 1000.0));
                e.put("channels", new JSONArray().put("emg0"));
                e.put("units", new JSONObject().put("emg0", "adc_counts"));
                JSONArray rows = new JSONArray();
                for (int i = 0; i < 50; i++) rows.put(new JSONArray().put((int) Math.max(0, Math.min(4095, 1950 + amp * rng.nextGaussian()))));
                e.put("samples", rows);
                e.put("quality", new JSONObject().put("flags", new JSONArray()));
                emgIndex += 50;
                emit(e);
            }
            while (imuIndex / 500.0 < elapsed - 0.05) {
                JSONObject m = base("imu_batch");
                m.put("sample_rate_hz", 500.0);
                m.put("first_sample_index", imuIndex);
                m.put("timing", new JSONObject().put("mode", "uniform").put("t0_us", imuIndex * 2000.0).put("interval_us", 2000.0));
                m.put("channels", new JSONArray().put("ax").put("ay").put("az"));
                m.put("units", new JSONObject().put("ax", "raw_counts").put("ay", "raw_counts").put("az", "raw_counts"));
                JSONArray rows = new JSONArray();
                for (int i = 0; i < 25; i++) {
                    double t = (imuIndex + i) / 500.0;
                    double ax = 3 * rng.nextGaussian(), ay = 3 * rng.nextGaussian(), az = 4096 + 3 * rng.nextGaussian();
                    if (armMoving) {
                        ax += 3000 * Math.sin(2 * Math.PI * 2.5 * t) + 1200 * Math.sin(2 * Math.PI * 0.9 * t);
                        if (knockLeft == 0 && rng.nextDouble() < 0.01) knockLeft = 10;
                        if (knockLeft > 0) { ax += knockLeft * 600 * Math.sin(2 * Math.PI * 70 * t); knockLeft--; }
                    }
                    if (tool) { ax += 2400 * Math.sin(2 * Math.PI * 120 * t); ay += 900 * Math.sin(2 * Math.PI * 240 * t); }
                    rows.put(new JSONArray().put(clamp(ax)).put(clamp(ay)).put(clamp(az)));
                }
                m.put("samples", rows);
                m.put("quality", new JSONObject().put("flags", new JSONArray()));
                imuIndex += 25;
                emit(m);
            }
        } catch (Exception ignored) {
        }
    }

    private static int clamp(double v) { return (int) Math.max(-32768, Math.min(32767, v)); }

    private void emit(JSONObject o) { if (o != null) out.accept(o.toString()); }
}
