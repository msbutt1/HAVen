package com.haven.app.engine;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.util.Random;

/**
 * Grip setup on a synthetic sleeve (same record format as board W): the cases that made the relax step fail
 * on the real sleeve.
 */
public class GripSetupTest {
    static final int RATE = 1000, BATCH = 50;

    /** A fake board W: EMG at 1 kHz in 50-sample batches, values 0..4095 declared as the raw limits. */
    static final class Sleeve {
        final Processor proc = new Processor(4096);
        final Random rnd = new Random(7);
        long seq = 0, index = 0;
        double clockS = 0;

        Sleeve() throws Exception {
            JSONObject emg = new JSONObject().put("nominal_rate_hz", RATE).put("channels", new JSONArray().put("emg0"))
                    .put("raw_limits", new JSONObject().put("min", 0).put("max", 4095));
            feed(base("hello").put("t_us", 1000).put("firmware", new JSONObject().put("name", "vibeguard-fw").put("version", "test"))
                    .put("streams", new JSONObject().put("emg", emg)));
        }

        JSONObject base(String type) throws Exception {
            return new JSONObject().put("schema", "vibeguard.device_telemetry").put("version", 1).put("type", type)
                    .put("device_id", "vg-w-test").put("boot_id", "b-test").put("seq", seq++)
                    .put("provenance", new JSONObject().put("source", "live"));
        }

        void feed(JSONObject o) { assertTrue(proc.feedLine(o.toString(), clockS)); }

        /** One batch: noise of the given amplitude around mid-scale; spikeAt >= 0 puts one sample at 0 and flags the batch. */
        void batch(double amp, int spikeAt, boolean skip) throws Exception {
            if (skip) { index += BATCH; clockS += BATCH / (double) RATE; return; }   // lost on Wi-Fi
            JSONArray rows = new JSONArray();
            for (int i = 0; i < BATCH; i++) {
                double v = i == spikeAt ? 0 : Math.max(1, Math.min(4094, 1500 + amp * rnd.nextGaussian()));
                rows.put(new JSONArray().put(Math.round(v)));
            }
            JSONObject b = base("emg_batch").put("sample_rate_hz", RATE).put("first_sample_index", index)
                    .put("timing", new JSONObject().put("mode", "uniform").put("t0_us", 1_000_000L + index * 1000).put("interval_us", 1000))
                    .put("channels", new JSONArray().put("emg0")).put("units", new JSONObject().put("emg0", "adc_counts"))
                    .put("samples", rows)
                    .put("quality", new JSONObject().put("flags", spikeAt >= 0 ? new JSONArray().put("clipped") : new JSONArray()));
            index += BATCH;
            clockS += BATCH / (double) RATE;
            feed(b);
            proc.poll(clockS);
        }

        void seconds(double s, double amp) throws Exception { for (int i = 0; i < s * RATE / BATCH; i++) batch(amp, -1, false); }
    }

    @Test
    public void oneSpikePerBatchDoesNotFailTheRelaxStep() throws Exception {
        Sleeve s = new Sleeve();
        s.seconds(2, 20);
        s.proc.gripCalibration("relaxed");
        // a single saturated sample in every 4th batch: the device flags the whole batch "clipped". Before the fix
        // all 50 samples counted as clipped (25 % of the capture > the 10 % allowance) and the step failed.
        for (int i = 0; i < 80; i++) s.batch(20, i % 4 == 0 ? 10 : -1, false);
        assertEquals("relaxed_captured", s.proc.snapshot.calibrationState);
    }

    @Test
    public void aShortWifiGapPausesTheCaptureInsteadOfFailingIt() throws Exception {
        Sleeve s = new Sleeve();
        s.seconds(2, 20);
        s.proc.gripCalibration("relaxed");
        for (int i = 0; i < 20; i++) s.batch(20, -1, false);
        s.batch(20, -1, true);                 // one batch (50 ms) lost
        for (int i = 0; i < 60; i++) s.batch(20, -1, false);
        assertEquals("relaxed_captured", s.proc.snapshot.calibrationState);
    }

    @Test
    public void recalibratingAfterASuccessfulCalibrationWorks() throws Exception {
        Sleeve s = new Sleeve();
        s.seconds(2, 20);
        for (int round = 0; round < 3; round++) {
            s.proc.gripCalibration("relaxed");
            s.seconds(4, 20);
            assertEquals("round " + round, "relaxed_captured", s.proc.snapshot.calibrationState);
            s.proc.gripCalibration("squeeze");
            s.seconds(4, 300);
            assertEquals("round " + round, "calibrated", s.proc.snapshot.calibrationState);
            s.seconds(2, 20);
        }
    }

    @Test
    public void aNoisyRestFailsAtTheRelaxStepWithItsOwnReason() throws Exception {
        Sleeve s = new Sleeve();
        s.seconds(2, 600);                     // ~600 counts of noise with the arm "relaxed" (seen live 2026-11-09)
        s.proc.gripCalibration("relaxed");
        s.seconds(4, 600);
        assertEquals("failed", s.proc.snapshot.calibrationState);
        assertEquals("noisy_at_rest", s.proc.snapshot.calibrationReason);
    }

    @Test
    public void saturationIsReportedAndClearsWhenTheSignalComesOffTheLimit() throws Exception {
        Sleeve s = new Sleeve();
        s.seconds(2, 20);
        assertTrue(s.proc.snapshot.emgSaturation < 0.02);
        // the amplifier pinned at 0 for a second (after a hard squeeze)
        for (int i = 0; i < 20; i++) {
            JSONArray rows = new JSONArray();
            for (int k = 0; k < BATCH; k++) rows.put(new JSONArray().put(0));
            s.feed(s.base("emg_batch").put("sample_rate_hz", RATE).put("first_sample_index", s.index)
                    .put("timing", new JSONObject().put("mode", "uniform").put("t0_us", 1_000_000L + s.index * 1000).put("interval_us", 1000))
                    .put("channels", new JSONArray().put("emg0")).put("units", new JSONObject().put("emg0", "adc_counts"))
                    .put("samples", rows).put("quality", new JSONObject().put("flags", new JSONArray().put("clipped"))));
            s.index += BATCH; s.clockS += 0.05;
        }
        assertTrue("saturation " + s.proc.snapshot.emgSaturation, s.proc.snapshot.emgSaturation > 0.5);
        s.seconds(3, 20);
        assertTrue("saturation " + s.proc.snapshot.emgSaturation, s.proc.snapshot.emgSaturation < 0.02);
    }
}
