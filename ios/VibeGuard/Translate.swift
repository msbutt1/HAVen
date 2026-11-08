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

    static let calibrationReasonText: [String: String] = [
        "insufficient_separation": "The squeeze was not clearly stronger than the relaxed reading. Squeeze harder and try again.",
        "insufficient_data": "Not enough clean signal was captured. Check the electrodes and try again.",
        "clipping": "The signal was clipping (too strong for the sensor). Reduce the gain or electrode pressure and try again.",
        "device_disconnected": "The sleeve disconnected during calibration.",
        "boot_change": "The sleeve restarted during calibration.",
    ]

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
        var activation: Double? = nil
        if norm["status"].string == "ok", let d = norm["display"].firstNumber {
            activation = (d * 1000).rounded() / 10            // 0...1 -> percent, one decimal
        }
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
            peakVibration: readings["peak_vibration_ms2"].double)
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
