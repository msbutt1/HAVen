import Foundation

/// Accelerometer vibration: per-axis 5 Hz high-pass (removes gravity and slow motion), RMS over 1 s,
/// combined vector magnitude in m/s^2. Port of processing/vibration.py. Unweighted, not ISO 5349.
struct VibrationState {
    var status: String               // ok | degraded | warming_up | unavailable
    var reasons: [String]
    var combinedRms: Double?         // m/s^2 (physical)
}

final class VibrationFeatures {
    let highpassHz = 5.0, order = 2, windowS = 1.0, settleS = 0.25
    let scale: Double                 // m/s^2 per count
    private(set) var rate: Double?
    private var filter: SOSFilter?
    private var x0: [Double]?
    private var since = 0
    private var nWin = 1, nSettle = 0
    private var sq: [[Double]] = [[], [], []]   // squared filtered values, per axis
    private var clip: [Bool] = []
    private(set) var resetReason = "no_data"

    init(countsPerG: Double) { scale = 9.80665 / countsPerG }

    func configure(rate: Double) {
        guard rate != self.rate else { return }
        self.rate = rate
        filter = SOSFilter(Butterworth.design(order: order, .highpass(highpassHz), fs: rate), channels: 3)
        nWin = max(1, Int((windowS * rate).rounded(.toNearestOrEven)))
        nSettle = Int((settleS * rate).rounded(.toNearestOrEven))
        reset("configured")
    }

    func reset(_ reason: String) {
        x0 = nil
        filter?.reset()
        since = 0
        sq = [[], [], []]
        clip.removeAll(keepingCapacity: true)
        resetReason = reason
    }

    func process(_ rows: [[Double]?], clipped: [Bool]) {
        guard filter != nil else { return }
        for (i, r) in rows.enumerated() {
            guard let r else { reset("missing_values"); continue }
            if x0 == nil { x0 = r }
            since += 1
            var ys = [0.0, 0.0, 0.0]
            for a in 0..<3 { ys[a] = filter!.step(r[a] - x0![a], channel: a) }
            if since > nSettle {
                for a in 0..<3 {
                    sq[a].append(ys[a] * ys[a])
                    if sq[a].count > nWin { sq[a].removeFirst() }
                }
                clip.append(clipped[i])
                if clip.count > nWin { clip.removeFirst() }
            }
        }
    }

    var state: VibrationState {
        if rate == nil || since == 0 {
            return VibrationState(status: "unavailable", reasons: [resetReason == "configured" ? "no_data" : resetReason], combinedRms: nil)
        }
        if sq[0].count < nWin {
            var r = ["warming_up"]
            if resetReason != "no_data" && resetReason != "configured" { r.append(resetReason) }
            return VibrationState(status: "warming_up", reasons: r, combinedRms: nil)
        }
        let n = Double(sq[0].count)
        var total = 0.0
        for a in 0..<3 {
            let rms = (max(0, sq[a].reduce(0, +)) / n).squareRoot()
            total += (scale * rms) * (scale * rms)
        }
        let clipped = clip.contains(true)
        return VibrationState(status: clipped ? "degraded" : "ok", reasons: clipped ? ["clipped"] : [],
                              combinedRms: NP.round(total.squareRoot(), 4))
    }
}
