package com.vibeguard.app.engine;

import java.util.Collections;
import java.util.Map;

/** Everything the engine reports besides the continuous values (as the Swift EngineEvent). */
public final class Event {
    public enum Kind { CALIBRATION, CALIBRATION_REJECTED, TOOL_CALIBRATION, TOOL_CALIBRATION_REJECTED,
                       BOOT_CHANGE, SEQ_GAP, SAMPLE_GAP, STREAM_STALE, STREAM_RECOVERED, SENSOR_STATE }

    public final Kind kind;
    public final String state, reason, stream;
    public final long missing;
    public final Map<String, Double> detail;
    public final ToolUse.CalResult result;

    private Event(Kind kind, String state, String reason, String stream, long missing,
                  Map<String, Double> detail, ToolUse.CalResult result) {
        this.kind = kind; this.state = state; this.reason = reason; this.stream = stream;
        this.missing = missing; this.detail = detail == null ? Collections.emptyMap() : detail; this.result = result;
    }

    static Event calibration(String state, String reason, Map<String, Double> detail) {
        return new Event(Kind.CALIBRATION, state, reason, null, 0, detail, null);
    }
    static Event calibrationRejected(String phase, String reason) {
        return new Event(Kind.CALIBRATION_REJECTED, phase, reason, null, 0, null, null);
    }
    static Event toolCalibration(String state, String reason, ToolUse.CalResult r) {
        return new Event(Kind.TOOL_CALIBRATION, state, reason, null, 0, null, r);
    }
    static Event toolCalibrationRejected(String reason) {
        return new Event(Kind.TOOL_CALIBRATION_REJECTED, null, reason, null, 0, null, null);
    }
    static Event bootChange(String previous) { return new Event(Kind.BOOT_CHANGE, previous, null, null, 0, null, null); }
    static Event seqGap(long missing) { return new Event(Kind.SEQ_GAP, null, null, null, missing, null, null); }
    static Event sampleGap(String stream, long missing) { return new Event(Kind.SAMPLE_GAP, null, null, stream, missing, null, null); }
    static Event stale(String stream) { return new Event(Kind.STREAM_STALE, null, null, stream, 0, null, null); }
    static Event recovered(String stream) { return new Event(Kind.STREAM_RECOVERED, null, null, stream, 0, null, null); }
    static Event sensor(String stream, String state) { return new Event(Kind.SENSOR_STATE, state, null, stream, 0, null, null); }
}
