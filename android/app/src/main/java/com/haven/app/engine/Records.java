package com.haven.app.engine;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/**
 * Device telemetry contract v1 (signals/contract/TELEMETRY_CONTRACT.md): the records the sleeve sends,
 * one JSON object per line. Only what the processor uses is decoded; a line that does not parse is
 * rejected whole, never repaired.
 */
public final class Records {
    private Records() {}

    public static final String SCHEMA = "vibeguard.device_telemetry";
    public static final int MAX_SAMPLES_PER_BATCH = 256;

    public static final class StreamDecl {
        public Double nominalRateHz;
        public double[] rawLimits;          // {min, max} or null
        public String sensorModel;          // the board's own description of the sensor, or null
    }

    public static final class Batch {
        public String stream;               // "emg" | "imu"
        public double sampleRateHz;
        public long firstSampleIndex;
        public double[] times;              // device time of each row, microseconds
        public Double timingHz;
        public String[] channels;
        public Double[][] samples;          // null = missing value
        public String[] flags;
        public boolean hasFlag(String f) { for (String x : flags) if (x.equals(f)) return true; return false; }
        public int indexOf(String ch) { for (int i = 0; i < channels.length; i++) if (channels[i].equals(ch)) return i; return -1; }
    }

    public static final class Record {
        public String deviceId, bootId, source, type, firmware;
        public long seq;
        public Map<String, StreamDecl> streams;   // hello
        public Map<String, String> sensors;        // status
        public Batch batch;                        // emg_batch / imu_batch
    }

    public static Record parse(String line) {
        try {
            return parse(new JSONObject(line));
        } catch (Exception e) {     // JSONException is checked on Android, unchecked in desktop org.json
            return null;
        }
    }

    static Record parse(JSONObject o) throws JSONException {
        if (!SCHEMA.equals(o.optString("schema")) || o.optInt("version", -1) != 1) return null;
        Record r = new Record();
        r.type = o.optString("type");
        r.deviceId = o.optString("device_id");
        r.bootId = o.optString("boot_id");
        if (r.deviceId.isEmpty() || r.bootId.isEmpty() || !o.has("seq")) return null;
        r.seq = o.getLong("seq");
        JSONObject prov = o.optJSONObject("provenance");
        if (prov == null) return null;
        r.source = prov.optString("source");
        if (!r.source.equals("live") && !r.source.equals("simulated") && !r.source.equals("replay")) return null;
        switch (r.type) {
            case "hello": {
                r.streams = new HashMap<>();
                JSONObject s = o.optJSONObject("streams");
                if (s != null) {
                    for (Iterator<String> it = s.keys(); it.hasNext(); ) {
                        String name = it.next();
                        JSONObject d = s.optJSONObject(name);
                        StreamDecl decl = new StreamDecl();
                        if (d != null) {
                            if (d.has("nominal_rate_hz") && !d.isNull("nominal_rate_hz")) decl.nominalRateHz = d.getDouble("nominal_rate_hz");
                            decl.sensorModel = d.optString("sensor_model", null);
                            JSONObject l = d.optJSONObject("raw_limits");
                            if (l != null && l.has("min") && l.has("max") && l.getDouble("min") < l.getDouble("max")) {
                                decl.rawLimits = new double[]{l.getDouble("min"), l.getDouble("max")};
                            }
                        }
                        r.streams.put(name, decl);
                    }
                }
                JSONObject fw = o.optJSONObject("firmware");
                r.firmware = fw == null ? null : fw.optString("version", null);
                return r;
            }
            case "status": {
                r.sensors = new HashMap<>();
                JSONObject s = o.optJSONObject("sensors");
                if (s != null) for (Iterator<String> it = s.keys(); it.hasNext(); ) {
                    String k = it.next();
                    r.sensors.put(k, s.optString(k));
                }
                return r;
            }
            case "emg_batch":
            case "imu_batch":
                r.batch = batch(o, r.type.equals("emg_batch") ? "emg" : "imu");
                return r.batch == null ? null : r;
            default:
                return null;
        }
    }

    private static Batch batch(JSONObject o, String stream) throws JSONException {
        Batch b = new Batch();
        b.stream = stream;
        b.sampleRateHz = o.getDouble("sample_rate_hz");
        b.firstSampleIndex = o.getLong("first_sample_index");
        JSONArray ch = o.getJSONArray("channels");
        b.channels = new String[ch.length()];
        for (int i = 0; i < ch.length(); i++) b.channels[i] = ch.getString(i);
        JSONObject units = o.getJSONObject("units");
        if (b.sampleRateHz <= 0 || b.channels.length == 0 || units.length() != b.channels.length) return null;
        JSONArray rows = o.getJSONArray("samples");
        int n = rows.length();
        if (n == 0 || n > MAX_SAMPLES_PER_BATCH) return null;
        b.samples = new Double[n][];
        for (int i = 0; i < n; i++) {
            JSONArray row = rows.getJSONArray(i);
            if (row.length() != b.channels.length) return null;
            Double[] v = new Double[row.length()];
            for (int c = 0; c < v.length; c++) v[c] = row.isNull(c) ? null : row.getDouble(c);
            b.samples[i] = v;
        }
        JSONObject t = o.getJSONObject("timing");
        b.times = new double[n];
        if ("uniform".equals(t.optString("mode"))) {
            double t0 = t.getDouble("t0_us"), dt = t.getDouble("interval_us");
            if (dt <= 0) return null;
            for (int i = 0; i < n; i++) b.times[i] = t0 + i * dt;
            b.timingHz = 1e6 / dt;
        } else if ("explicit".equals(t.optString("mode"))) {
            JSONArray ts = t.getJSONArray("t_us");
            if (ts.length() != n) return null;
            for (int i = 0; i < n; i++) b.times[i] = ts.getDouble(i);
            b.timingHz = n >= 2 && b.times[n - 1] > b.times[0] ? (n - 1) * 1e6 / (b.times[n - 1] - b.times[0]) : null;
        } else {
            return null;
        }
        JSONObject q = o.optJSONObject("quality");
        JSONArray fl = q == null ? null : q.optJSONArray("flags");
        b.flags = new String[fl == null ? 0 : fl.length()];
        for (int i = 0; i < b.flags.length; i++) b.flags[i] = fl.getString(i);
        return b;
    }
}
