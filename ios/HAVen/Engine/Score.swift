import Foundation

/// EXPERIMENTAL exposure score (processing/score.py formula):
///   rate [au/h] = k * (V / v_ref)^2 * (1 + w * clamp(a, 0, 2))
/// V = combined vibration RMS in m/s^2, a = normalised muscle activation. Arbitrary units: not A(8),
/// not a vibration dose, not a percentage of any limit.
///
/// Interval bookkeeping is simplified from the Python engine: each vibration update covers the time since
/// the previous one (at most the 0.25 s freshness limit) and counts only while the latest activation is
/// usable and no older than 0.25 s. Anything else is excluded time, never scored.
final class ScoreEngine {
    var k = 1.0, vRef = 1.0, exponent = 2.0, weight = 1.0, cap = 2.0, freshS = 0.25

    private(set) var cumulative = 0.0
    private(set) var validS = 0.0
    private(set) var excludedS = 0.0
    private(set) var rate: Double?
    private(set) var pausedReason: String? = "no_data"
    private var lastVibT: Double?
    private var act: (t: Double, value: Double?)?

    func rateFor(_ v: Double, _ a: Double?) -> Double {
        k * pow(v / vRef, exponent) * (1 + weight * min(cap, max(0, a ?? 0)))
    }

    func updateActivation(_ t: Double?, _ n: Normalized) {
        guard let t else { return }
        act = (t, (n.status == "ok" || n.status == "degraded") ? n.value : nil)
    }

    func updateVibration(_ t: Double?, _ v: VibrationState) {
        guard let t else { return }
        defer { lastVibT = t }
        let vib = (v.status == "ok" || v.status == "degraded") ? v.combinedRms : nil
        var why: String?
        if vib == nil { why = "vibration_\(v.reasons.last ?? v.status)" }
        else if act == nil { why = "activation_no_data" }
        else if act!.value == nil { why = "activation_uncalibrated_or_unavailable" }
        else if t - act!.t > freshS * 1e6 { why = "activation_stale" }
        pausedReason = why
        rate = why == nil ? rateFor(vib!, act!.value) : nil
        guard let last = lastVibT, t > last else { return }
        let dt = min(t - last, freshS * 1e6) / 1e6
        if let r = rate { cumulative += r * dt / 3600; validS += dt } else { excludedS += dt }
    }

    func onBootChange() { lastVibT = nil; act = nil; rate = nil; pausedReason = "boot_change" }
    func resetAccumulation() { cumulative = 0; validS = 0; excludedS = 0 }
}

/// Everything the engine reports besides the continuous values.
enum EngineEvent: Equatable {
    case calibration(state: String, reason: String?, detail: [String: Double])
    case calibrationRejected(phase: String, reason: String)
    case toolCalibration(state: String, reason: String?, result: ToolCalibrationResult?)
    case toolCalibrationRejected(reason: String)
    case bootChange(previous: String?)
    case seqGap(missing: Int)
    case sampleGap(stream: String, missing: Int)
    case streamStale(stream: String)
    case streamRecovered(stream: String)
    case sensorState(stream: String, state: String)
}
