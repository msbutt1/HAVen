import Foundation

/// Tool use vs normal arm use, tool time and warnings. Port of processing/tool_use.py.
///
/// A window (0.5 s) is a tool candidate when the tool band (20 Hz high-pass) is loud enough, strong compared
/// with arm movement (0.5-10 Hz), and periodic (power in a few frequency lines). "tool" needs candidates for
/// 2 s with a steady dominant frequency, so handshakes, waves and knocks never count. Thresholds are
/// provisional team settings, or set per wearer by the tool calibration. Not regulatory limits, not a dose.
struct ToolConfig: Codable, Equatable {
    var toolBandLowHz = 20.0
    var motionLowHz = 0.5, motionHighHz = 10.0
    var order = 4
    var windowS = 0.5
    var onThreshold = 1.0            // m/s^2
    var offThreshold = 0.6
    var toolToMotionMin = 0.5
    var concentrationMin = 0.0
    var steadyMin = 0.6
    var steadyTolHz = 5.0
    var onHoldS = 2.0, offHoldS = 3.0, maxStepS = 0.5
    var continuousWarnS = 20.0 * 60, sessionWarnS = 2.0 * 3600, sessionNoticeFraction = 0.8
    var calStillS = 5.0, calMovementS = 12.0, calToolS = 10.0, calDiscardS = 1.0
    var calToolPassMin = 0.8, calMovementPassMax = 0.10
}

struct ToolFeatures { var tool: Double; var motion: Double; var dominantHz: Double?; var conc: Double }

struct ToolState: Equatable {
    var state: String                // tool | no_tool | unknown
    var candidate = false
    var toolBand: Double?
    var motionBand: Double?
    var dominantHz: Double?
    var concentration: Double?
    var steadiness: Double?
    var boutS = 0.0
    var bouts = 0
    var longestBoutS = 0.0
    var sessionToolS = 0.0
    var sessionObservedS = 0.0
    var warnings: [String] = []
    var calibrationState = "uncalibrated"
    var calibrationSource = "default"
    var captureLeftS: Double?
}

/// Summary of one tool-calibration capture (medians and 95th percentiles).
struct CaptureSummary: Codable, Equatable {
    struct Q: Codable, Equatable { var median: Double; var p95: Double }
    var windows: Int
    var toolBand: Q, motionBand: Q, ratio: Q, concentration: Q
}

struct ToolCalibrationResult: Codable, Equatable {
    var summaries: [String: CaptureSummary]
    var toolWindowsAccepted: Double
    var movementWindowsAccepted: Double
    var movementFalseBouts: Int
    var separates: [String: Bool]
    var thresholds: [String: Double]  // on_threshold_ms2, off_threshold_ms2, tool_to_motion_min, concentration_min
}

final class ToolUseDetector {
    let defaults: ToolConfig
    private(set) var cfg: ToolConfig
    private let scale: Double
    private(set) var rate: Double?
    private var hp: SOSFilter?, bp: SOSFilter?
    private var nWin = 0
    private var x0: [Double]?
    private var toolBuf: [[Double]] = [], motionBuf: [[Double]] = []
    private var hann: [Double] = []
    private var cosT: [[Double]] = [], sinT: [[Double]] = []
    private(set) var resetReason = "no_data"

    // session
    private(set) var state = "no_tool"
    private(set) var sessionToolS = 0.0, sessionObservedS = 0.0
    private(set) var bouts = 0
    private var boutS = 0.0, longestBoutS = 0.0, candS = 0.0, unobservedS = 0.0
    private var hist: [(Double, Bool)] = []
    private var doms: [(Double, Double?)] = []
    private var lastT: Double?

    // calibration
    private(set) var calState = "uncalibrated"
    private(set) var calSource = "default"
    private(set) var calResult: ToolCalibrationResult?
    private(set) var lastFailure: String?
    private var captures: [String: [(dt: Double, tool: Double, motion: Double, conc: Double, dom: Double?)]] = [:]
    private var calPhase: String?
    private var calElapsed = 0.0
    private var calBuf: [(dt: Double, tool: Double, motion: Double, conc: Double, dom: Double?)] = []
    var events: [EngineEvent] = []

    init(config: ToolConfig = ToolConfig(), countsPerG: Double) {
        defaults = config
        cfg = config
        scale = 9.80665 / countsPerG
    }

    func configure(rate: Double) {
        guard rate != self.rate else { return }
        self.rate = rate
        hp = SOSFilter(Butterworth.design(order: cfg.order, .highpass(cfg.toolBandLowHz), fs: rate), channels: 3)
        bp = SOSFilter(Butterworth.design(order: 2, .bandpass(cfg.motionLowHz, cfg.motionHighHz), fs: rate), channels: 3)
        nWin = max(8, Int((cfg.windowS * rate).rounded(.toNearestOrEven)))
        let n = nWin
        hann = (0..<n).map { 0.5 - 0.5 * cos(2 * Double.pi * Double($0) / Double(n - 1)) }   // numpy.hanning
        let bins = n / 2 + 1
        cosT = (0..<bins).map { k in (0..<n).map { cos(2 * Double.pi * Double(k * $0 % n) / Double(n)) } }
        sinT = (0..<bins).map { k in (0..<n).map { sin(2 * Double.pi * Double(k * $0 % n) / Double(n)) } }
        reset("configured")
    }

    func reset(_ reason: String) {
        x0 = nil
        hp?.reset(); bp?.reset()
        toolBuf.removeAll(); motionBuf.removeAll()
        resetReason = reason
    }

    func process(_ rows: [[Double]?]) {
        guard hp != nil else { return }
        for r in rows {
            guard let r else { reset("missing_values"); continue }
            let x = r.map { $0 * scale }
            if x0 == nil { x0 = x }
            var t = [0.0, 0.0, 0.0], m = [0.0, 0.0, 0.0]
            for a in 0..<3 {
                t[a] = hp!.step(x[a] - x0![a], channel: a)
                m[a] = bp!.step(x[a] - x0![a], channel: a)
            }
            toolBuf.append(t); motionBuf.append(m)
            if toolBuf.count > 2 * nWin { toolBuf.removeFirst(); motionBuf.removeFirst() }
        }
    }

    private func features() -> ToolFeatures? {
        let n = nWin
        guard let rate, n > 0, toolBuf.count >= 2 * n else { return nil }
        let t = toolBuf.suffix(n), m = motionBuf.suffix(n)
        var tool = 0.0, motion = 0.0
        for a in 0..<3 {
            tool += t.reduce(0) { $0 + $1[a] * $1[a] } / Double(n)
            motion += m.reduce(0) { $0 + $1[a] * $1[a] } / Double(n)
        }
        // power spectrum of the Hann-windowed tool band, summed over axes (numpy rfft)
        let bins = n / 2 + 1
        var spec = [Double](repeating: 0, count: bins)
        let rows = Array(t)
        for a in 0..<3 {
            let w = (0..<n).map { rows[$0][a] * hann[$0] }
            for k in 0..<bins {
                var re = 0.0, im = 0.0
                let c = cosT[k], s = sinT[k]
                for i in 0..<n { re += w[i] * c[i]; im -= w[i] * s[i] }
                spec[k] += re * re + im * im
            }
        }
        var dom: Double?
        if bins > 1 {
            var best = 1
            for k in 1..<bins where spec[k] > spec[best] { best = k }
            dom = Double(best) * rate / Double(n)
        }
        let band = (0..<bins).filter { Double($0) * rate / Double(n) >= cfg.toolBandLowHz }.map { spec[$0] }
        let total = band.reduce(0, +)
        let kTop = max(3, Int((0.1 * Double(band.count)).rounded(.toNearestOrEven)))
        let conc = total > 0 && !band.isEmpty ? band.sorted().suffix(kTop).reduce(0, +) / total : 0
        return ToolFeatures(tool: tool.squareRoot(), motion: motion.squareRoot(), dominantHz: dom, conc: conc)
    }

    // MARK: session

    func resetSession() {
        state = "no_tool"
        sessionToolS = 0; sessionObservedS = 0; bouts = 0
        boutS = 0; longestBoutS = 0; candS = 0; unobservedS = 0
        hist.removeAll(); doms.removeAll()
        lastT = nil
    }

    func onBootChange() {
        lastT = nil
        if calState.hasPrefix("capturing") { calFail("interrupted_boot_change") }
    }

    func update(_ tUs: Double?) -> ToolState {
        var dt = 0.0
        if let t = tUs {
            if let l = lastT, t > l { dt = (t - l) / 1e6 }
            lastT = t
        }
        let f = rate != nil ? features() : nil
        if dt > cfg.maxStepS { unobservedS += dt; dt = 0 }
        if calState.hasPrefix("capturing") { calObserve(f, dt) }
        guard let f else {
            unobservedS += dt
            if state == "tool" && unobservedS >= cfg.offHoldS { endBout() }
            candS = 0
            return record("unknown", nil, false, nil)
        }
        unobservedS = 0
        sessionObservedS += dt
        let ratioOK = f.tool >= cfg.toolToMotionMin * f.motion && f.conc >= cfg.concentrationMin
        let candidate = f.tool >= cfg.onThreshold && ratioOK
        let active = f.tool >= cfg.offThreshold && ratioOK
        var steady: Double?
        if state == "no_tool" {
            candS = candidate ? candS + dt : 0
            if candidate {
                doms.append((dt, f.dominantHz))
                while !doms.isEmpty && doms.reduce(0, { $0 + $1.0 }) - doms[0].0 >= cfg.onHoldS { doms.removeFirst() }
            } else {
                doms.removeAll()
            }
            steady = steadiness()
            if candidate && candS >= cfg.onHoldS && (steady ?? 0) >= cfg.steadyMin {
                state = "tool"
                bouts += 1
                boutS = candS
                sessionToolS += candS
                hist.removeAll()
            }
        } else {
            boutS += dt
            if active { sessionToolS += dt }
            hist.append((dt, active))
            var span = hist.reduce(0) { $0 + $1.0 }
            while !hist.isEmpty && span - hist[0].0 >= cfg.offHoldS { span -= hist.removeFirst().0 }
            let on = hist.filter(\.1).reduce(0) { $0 + $1.0 }
            if span >= cfg.offHoldS && on < 0.3 * span { endBout() }
        }
        longestBoutS = max(longestBoutS, state == "tool" ? boutS : 0)
        return record(state, f, candidate, steady)
    }

    private func steadiness() -> Double? {
        let hz = doms.compactMap(\.1)
        guard !hz.isEmpty else { return nil }
        let med = NP.median(hz)
        let tol = max(cfg.steadyTolHz, 0.1 * med)
        return Double(hz.filter { abs($0 - med) <= tol }.count) / Double(hz.count)
    }

    private func endBout() {
        state = "no_tool"
        longestBoutS = max(longestBoutS, boutS)
        boutS = 0
        hist.removeAll(); doms.removeAll()
        candS = 0
    }

    private func record(_ st: String, _ f: ToolFeatures?, _ candidate: Bool, _ steady: Double?) -> ToolState {
        var w: [String] = []
        if state == "tool" && boutS >= cfg.continuousWarnS { w.append("continuous_tool_use_long") }
        if sessionToolS >= cfg.sessionWarnS { w.append("session_tool_time_high") }
        else if sessionToolS >= cfg.sessionNoticeFraction * cfg.sessionWarnS { w.append("session_tool_time_approaching") }
        let r = { (v: Double?) in v.map { NP.round($0, 3) } }
        return ToolState(state: st, candidate: candidate, toolBand: r(f?.tool), motionBand: r(f?.motion),
                         dominantHz: f?.dominantHz.map { NP.round($0, 1) }, concentration: r(f?.conc),
                         steadiness: r(steady), boutS: state == "tool" ? boutS : 0, bouts: bouts,
                         longestBoutS: max(longestBoutS, state == "tool" ? boutS : 0),
                         sessionToolS: sessionToolS, sessionObservedS: sessionObservedS, warnings: w,
                         calibrationState: calState, calibrationSource: calSource, captureLeftS: captureLeftS)
    }

    // MARK: tool calibration (still -> movement -> tool)

    var captureLeftS: Double? {
        guard calState.hasPrefix("capturing"), let p = calPhase else { return nil }
        return max(0, duration(p) - calElapsed)
    }
    func duration(_ phase: String) -> Double {
        phase == "still" ? cfg.calStillS : phase == "movement" ? cfg.calMovementS : cfg.calToolS
    }

    func calibrationStart(_ phase: String) {
        guard ["still", "movement", "tool"].contains(phase) else {
            return events.append(.toolCalibrationRejected(reason: "unknown_phase"))
        }
        if phase == "tool" && captures["movement"] == nil {
            return events.append(.toolCalibrationRejected(reason: "movement_capture_required_first"))
        }
        calPhase = phase
        calElapsed = 0
        captures[phase] = nil
        calBuf = []
        calState = "capturing_\(phase)"
        events.append(.toolCalibration(state: calState, reason: nil, result: nil))
    }

    func calibrationCancel() {
        guard calState.hasPrefix("capturing") else { return }
        calPhase = nil
        calState = "cancelled"
        events.append(.toolCalibration(state: calState, reason: nil, result: nil))
    }

    func calibrationReset() {
        calPhase = nil
        cfg = defaults
        calState = "uncalibrated"; calSource = "default"
        captures = [:]; calResult = nil
        events.append(.toolCalibration(state: calState, reason: "reset_to_defaults", result: nil))
    }

    /// Restore a saved calibration (thresholds only).
    func install(_ result: ToolCalibrationResult) {
        guard let on = result.thresholds["on_threshold_ms2"], let off = result.thresholds["off_threshold_ms2"],
              let ratio = result.thresholds["tool_to_motion_min"], let conc = result.thresholds["concentration_min"],
              off > 0, off <= on else { return }
        cfg = defaults
        cfg.onThreshold = on; cfg.offThreshold = off; cfg.toolToMotionMin = ratio; cfg.concentrationMin = conc
        calResult = result
        calState = "calibrated"; calSource = "calibrated"
        events.append(.toolCalibration(state: calState, reason: "restored", result: result))
    }

    private func calFail(_ reason: String) {
        lastFailure = reason
        calPhase = nil
        calState = "failed"
        events.append(.toolCalibration(state: calState, reason: reason, result: calResult))
    }

    private func calObserve(_ f: ToolFeatures?, _ dt: Double) {
        guard let phase = calPhase else { return }
        guard let f else {
            if calElapsed > 0 { calFail("interrupted_data_gap") }
            return
        }
        calElapsed += dt
        if calElapsed > cfg.calDiscardS { calBuf.append((dt, f.tool, f.motion, f.conc, f.dominantHz)) }
        guard calElapsed >= duration(phase) else { return }
        captures[phase] = calBuf
        calPhase = nil
        if calBuf.count < 10 { calResult = nil; return calFail("insufficient_data") }
        if phase == "tool" { solve() } else {
            calState = "\(phase)_captured"
            events.append(.toolCalibration(state: calState, reason: nil, result: nil))
        }
    }

    private static func summary(_ rows: [(dt: Double, tool: Double, motion: Double, conc: Double, dom: Double?)]) -> CaptureSummary {
        let t = rows.map(\.tool), m = rows.map(\.motion), c = rows.map(\.conc)
        let ratio = rows.map { $0.tool / max($0.motion, 1e-6) }
        let q = { (v: [Double]) in CaptureSummary.Q(median: NP.round(NP.percentile(v, 50), 3), p95: NP.round(NP.percentile(v, 95), 3)) }
        return CaptureSummary(windows: rows.count, toolBand: q(t), motionBand: q(m), ratio: q(ratio), concentration: q(c))
    }

    private func solve() {
        let mv = captures["movement"]!, tl = captures["tool"]!, st = captures["still"]
        var summaries: [String: CaptureSummary] = [:]
        for (k, v) in captures { summaries[k] = Self.summary(v) }
        let floor = st.map { max(0.3, 3 * NP.percentile($0.map(\.tool), 90)) } ?? 0.3
        func failEarly(_ why: String) {
            calResult = ToolCalibrationResult(summaries: summaries, toolWindowsAccepted: 0, movementWindowsAccepted: 0,
                                              movementFalseBouts: 0, separates: [:], thresholds: [:])
            calFail(why)
        }
        if NP.median(tl.map(\.tool)) < max(floor, 0.5) { return failEarly("no_tool_vibration_detected") }
        if let st, NP.median(mv.map(\.motion)) < max(0.3, 3 * NP.median(st.map(\.motion))) {
            return failEarly("no_movement_detected")
        }
        let feats: [(String, [Double], [Double])] = [
            ("tool_band", mv.map(\.tool), tl.map(\.tool)),
            ("ratio", mv.map { $0.tool / max($0.motion, 1e-6) }, tl.map { $0.tool / max($0.motion, 1e-6) }),
            ("concentration", mv.map(\.conc), tl.map(\.conc)),
        ]
        var thr: [String: Double] = [:], sep: [String: Bool] = [:]
        for (name, m, t) in feats {
            let mHi = NP.percentile(m, 95), tLo = NP.percentile(t, 15)
            sep[name] = tLo > mHi
            if tLo > mHi { thr[name] = name == "concentration" ? (mHi + tLo) / 2 : (mHi * tLo).squareRoot() }
            else { thr[name] = 0.8 * NP.percentile(t, 10) }
        }
        let on = max(thr["tool_band"]!, floor)
        var new = cfg
        new.onThreshold = NP.round(on, 3)
        new.offThreshold = NP.round(max(min(0.6 * on, on), min(floor, on)), 3)
        new.toolToMotionMin = NP.round(thr["ratio"]!, 3)
        new.concentrationMin = NP.round(min(max(thr["concentration"]!, 0), 1), 3)
        func passes(_ r: (dt: Double, tool: Double, motion: Double, conc: Double, dom: Double?)) -> Bool {
            r.tool >= new.onThreshold && r.tool >= new.toolToMotionMin * r.motion && r.conc >= new.concentrationMin
        }
        let mp = mv.map(passes), tp = tl.map(passes)
        // replay the movement capture through on-hold + steadiness: would any of it start a bout?
        var falseBouts = 0, run = 0.0
        var dq: [(Double, Double?)] = []
        for (ok, row) in zip(mp, mv) {
            if !ok { run = 0; dq = []; continue }
            run += row.dt
            dq.append((row.dt, row.dom))
            while !dq.isEmpty && dq.reduce(0, { $0 + $1.0 }) - dq[0].0 >= cfg.onHoldS { dq.removeFirst() }
            let hz = dq.compactMap(\.1)
            let med = hz.isEmpty ? 0 : NP.median(hz)
            let tol = max(cfg.steadyTolHz, 0.1 * med)
            let steady = hz.isEmpty ? 0 : Double(hz.filter { abs($0 - med) <= tol }.count) / Double(hz.count)
            if run >= cfg.onHoldS && steady >= cfg.steadyMin { falseBouts += 1; run = 0; dq = [] }
        }
        let result = ToolCalibrationResult(
            summaries: summaries,
            toolWindowsAccepted: NP.round(Double(tp.filter { $0 }.count) / Double(tp.count), 3),
            movementWindowsAccepted: NP.round(Double(mp.filter { $0 }.count) / Double(mp.count), 3),
            movementFalseBouts: falseBouts, separates: sep,
            thresholds: ["on_threshold_ms2": new.onThreshold, "off_threshold_ms2": new.offThreshold,
                         "tool_to_motion_min": new.toolToMotionMin, "concentration_min": new.concentrationMin])
        calResult = result
        if result.toolWindowsAccepted < cfg.calToolPassMin { return calFail("tool_not_reliably_detected") }
        if result.movementWindowsAccepted > cfg.calMovementPassMax || falseBouts > 0 { return calFail("movement_looks_like_tool") }
        cfg = new
        calState = "calibrated"; calSource = "calibrated"
        events.append(.toolCalibration(state: calState, reason: nil, result: result))
    }
}
