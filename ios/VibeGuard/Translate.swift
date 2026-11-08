import Foundation

/// Turns the backend's records into what the screens show. Pure functions (no state, no network), so the
/// rules can be unit-tested: they are the same as the web dashboard's `backend-adapter.js`.
enum Translate {
    static let endReasonText: [String: String] = [
        "stopped_by_user": "stopped",
        "end_of_input": "the replayed recording reached its end",
        "device_changed": "a different sleeve connected",
        "runtime_stopped": "the backend stopped",
        "runtime_error": "the backend hit an error",
        "backend_restart": "the backend restarted during it",
    ]

    // plain words for each calibration failure reason (firmware/INTEGRATION_FOR_ABDULJABBAR.md, section 3)
    private static let mismatch = "This calibration belongs to a different device or session. Recalibrate."
    static let calibrationReasonText: [String: String] = [
        "insufficient_separation": "The squeeze wasn’t clearly stronger than rest. Squeeze harder, or check the pads, and try again.",
        "insufficient_data": "Not enough signal during the capture. Check the pads and try again.",
        "interrupted_boot_change": "The wearable restarted during calibration. Start again.",
        "interrupted_processing_change": "The processing settings changed during calibration. Start again.",
        "channel_mismatch": mismatch, "device_mismatch": mismatch, "boot_mismatch": mismatch, "provenance_mismatch": mismatch,
        "processing_config_mismatch": "The calibration was made with different settings. Recalibrate.",
        "calibration_unverified": "The saved calibration is waiting for new data to confirm it is the same wearable.",
        "clipping": "The signal was clipping (too strong for the sensor). Check the pads and try again.",
        "device_disconnected": "The wearable disconnected during calibration. Start again.",
        "boot_change": "The wearable restarted. Calibrate again.",
    ]

    static let eventTextMap: [String: String] = [
        "boot_change": "The wearable restarted. Calibrate again.",
        "seq_gap": "Some records were lost on the way.",
        "sample_gap": "A gap in the samples.",
        "stream_stale": "No data from the wearable: values are hidden.",
        "stream_recovered": "Data resumed.",
        "invalid_record": "A record failed the checks and was rejected.",
        "tool_session_reset": "New shift: tool time reset.",
        "rate_mismatch": "The sample rate differs from what the wearable announced.",
    ]

    static let warningText: [String: String] = [
        "continuous_tool_use_long": "Long continuous tool use. Consider a break.",
        "session_tool_time_high": "Tool time this session is high.",
        "session_tool_time_approaching": "Tool time this session is getting high.",
    ]

    static func eventText(_ event: String, detail: JSON) -> String {
        if event == "calibration_state" {
            let state = detail["state"].string ?? ""
            if state == "failed" { return "Calibration failed: \((detail["reason"].string ?? "").replacingOccurrences(of: "_", with: " "))." }
            return "Calibration: \(state.replacingOccurrences(of: "_", with: " "))."
        }
        return eventTextMap[event] ?? event.replacingOccurrences(of: "_", with: " ")
    }

    static func toolUse(_ p: JSON, saved: Bool = false, savedAt: Date? = nil) -> ToolUse {
        ToolUse(state: p["state"].string ?? "unknown", status: p["status"].string ?? "unavailable",
                sessionToolS: p["session_tool_s"].double, bouts: p["bouts"].double.map { Int($0) },
                longestBoutS: p["longest_bout_s"].double, dominantHz: p["dominant_hz"].double,
                warnings: (p["warnings"].array ?? []).compactMap(\.string), saved: saved, savedAt: savedAt)
    }

    static func calibrationFailure(_ reason: String?) -> String {
        guard let reason else { return "Calibration did not complete. Try again." }
        return calibrationReasonText[reason] ?? reason.replacingOccurrences(of: "_", with: " ")
    }

    /// Live values from one EMG feature record. `imu` is the latest IMU feature record if it is under 2 s old.
    static func telemetry(emg p: JSON, imu: JSON?, score: JSON, status: JSON,
                          sessionActive: Bool, scoreBase: Double?) -> Telemetry {
        let act = p["features"]["emg_activation"]
        let norm = act["normalized"]
        let calState = act["calibration"]["state"].string ?? status["calibration"]["state"].string
        // grip effort: `value` is unbounded (may go above 100 %), `display` is clamped for bars; only while the
        // status is ok or degraded (clipped samples), and never a missing value as 0
        let usable = norm["status"].string == "ok" || norm["status"].string == "degraded"
        let activation = usable ? norm["value"].firstNumber.map { ($0 * 1000).rounded() / 10 } : nil
        let activationBar = usable ? norm["display"].firstNumber.map { ($0 * 1000).rounded() / 10 } : nil
        func flag(_ key: String) -> Bool { usable && (norm[key].object?.values.first?.bool ?? false) }
        var vibration: Double? = nil
        if let imu {
            let phys = imu["features"]["vibration"]["physical"]
            if phys["status"].string == "ok" { vibration = phys["combined_rms"].double }
        }
        let rate = score["rate"]["value"].double               // null while the score is paused
        var total: Double? = nil
        if sessionActive, let base = scoreBase, let c = score["cumulative"]["value"].double {
            total = max(0, c - base)
        }
        let prov = p["provenance"]
        let source = Provenance(prov["source"].string)
        return Telemetry(
            deviceID: p["device_id"].string,
            provenance: source,
            recordingID: source == .replay ? prov["recording_id"].string : nil,
            emgEnvelope: act["envelope"]["value"].firstNumber,
            activationPct: activation,
            activationBarPct: activationBar,
            aboveMax: flag("above_reference"),
            belowRest: flag("below_relaxed"),
            degraded: norm["status"].string == "degraded",
            vibration: vibration,
            scoreRate: rate,
            scoreTotal: total,
            calibrated: calState == "calibrated",
            imuClipped: (p["quality"]["clipped_samples"].double ?? 0) > 0)
    }

    /// What the sleeve chip says, from a runtime status.
    static func sleeveState(_ rt: JSON) -> DeviceState {
        if let err = rt["error"].string { return DeviceState(kind: .error, detail: err) }
        if let conn = rt["connection"].string, conn != "connected" {
            return DeviceState(kind: .disconnected,
                               detail: rt["connection_detail"].string ?? "The wearable is not connected to the backend.")
        }
        switch rt["state"].string {
        case "recording": return DeviceState(kind: .recording, detail: "")
        case "calibrating": return DeviceState(kind: .calibrating, detail: "")
        case "completed": return DeviceState(kind: .completed, detail: "")
        case "error": return DeviceState(kind: .error, detail: "")
        case "disconnected": return DeviceState(kind: .disconnected, detail: "")
        default:
            return rt["active_session_id"].string != nil ? DeviceState(kind: .recording, detail: "")
                                                          : DeviceState(kind: .ready, detail: "")
        }
    }

    static func sessionRecord(_ row: JSON) -> SessionRecord? {
        guard let id = row["session_id"].string else { return nil }
        let started = date(row["started_at"].string)
        let ended = date(row["ended_at"].string)
        let atStart = row["score"]["at_start"]["cumulative_au"].double
        let atEnd = row["score"]["at_end"]["cumulative_au"].double
        let calibrated = row["calibration_id"].string != nil
        var total: Double? = nil
        if calibrated, let a = atStart, let e = atEnd { total = max(0, e - a) }
        let readings = row["counters"]["readings"]
        let reason = row["end_reason"].string ?? "stopped"
        return SessionRecord(
            id: id, deviceID: row["device_id"].string, provenance: Provenance(row["source"].string),
            startedAt: started, endedAt: ended,
            durationS: (started != nil && ended != nil) ? max(0, ended!.timeIntervalSince(started!)) : 0,
            endedReason: endReasonText[reason] ?? reason.replacingOccurrences(of: "_", with: " "),
            calibrated: calibrated, scoreTotal: total,
            meanActivationPct: readings["mean_activation_pct"].double,
            peakActivationPct: readings["peak_activation_pct"].double,
            peakVibration: readings["peak_vibration_ms2"].double,
            toolTimeS: readings["tool_time_s"].double)
    }

    static func stationRecord(_ row: JSON) -> StationResult? {
        guard let id = row["station_session_id"].string else { return nil }
        let mode: StationMode = row["protocol"]["mode"].string == "full" ? .full : .preview
        let trials: [TrialResult] = (row["trials"].array ?? []).enumerated().map { i, t in
            let isCatch = t["is_catch"].bool ?? false
            return TrialResult(id: t["trial_id"].string ?? "\(id)-\(i + 1)", index: i + 1,
                               setting: isCatch ? 0 : Int(t["stimulus"]["motor_setting"].double ?? 0),
                               isCatch: isCatch, response: t["response"].string ?? "missing",
                               rtMs: t["response_latency_ms"].double.map { Int($0.rounded()) })
        }
        return StationResult(id: id, provenance: Provenance(row["source"].string), mode: mode,
                             startedAt: date(row["started_at"].string), completedAt: date(row["ended_at"].string),
                             trials: trials, summary: Staircase.summarize(trials, mode: mode))
    }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain = ISO8601DateFormatter()

    static func date(_ s: String?) -> Date? {
        guard let s else { return nil }
        return iso.date(from: s) ?? isoPlain.date(from: s)
    }
}
