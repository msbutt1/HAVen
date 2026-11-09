import Foundation

/// EMG envelope: offset removal (slow running mean) -> 20-450 Hz band-pass -> 100 ms moving RMS.
/// Port of processing/emg.py. A relative signal-processing proxy in ADC counts, not force, not %MVC.
struct EnvelopeState {
    var status: String               // ok | degraded | warming_up | unavailable
    var reasons: [String]
    var value: Double?               // channel emg0 (the sleeve has one EMG channel)
    var samplesTotal: Int
    var clippedTotal: Int
}

final class EmgEnvelope {
    let lowHz = 20.0, highHz = 450.0, order = 2
    let offsetTau = 1.0, windowS = 0.1, settleS = 0.1

    private(set) var rate: Double?
    private var filter: SOSFilter?
    private var alpha = 0.0
    private var nEnv = 1, nSettle = 0
    private var ema: Double?
    private var since = 0
    private var tail: [Double] = []
    private var tailClip: [Bool] = []
    private(set) var resetReason = "no_data"
    private(set) var configError: String?
    private(set) var samplesTotal = 0
    private(set) var clippedTotal = 0
    var onReset: ((String) -> Void)?

    func configure(rate: Double) {
        guard rate != self.rate else { return }
        self.rate = rate
        configError = highHz > 0.9 * rate / 2 ? "band-pass upper edge above 0.9 x Nyquist" : nil
        filter = configError == nil ? SOSFilter(Butterworth.design(order: order, .bandpass(lowHz, highHz), fs: rate), channels: 1) : nil
        nEnv = max(1, Int((windowS * rate).rounded(.toNearestOrEven)))
        nSettle = Int((settleS * rate).rounded(.toNearestOrEven))
        alpha = exp(-1 / (rate * offsetTau))
        reset("configured")
    }

    func reset(_ reason: String) {
        ema = nil
        filter?.reset()
        since = 0
        tail.removeAll(keepingCapacity: true)
        tailClip.removeAll(keepingCapacity: true)
        resetReason = reason
        onReset?(reason)
    }

    /// One batch of contiguous rows for channel emg0 (nil = missing sample: the filter never runs across it).
    func process(_ values: [Double?], clipped: [Bool]) {
        guard configError == nil, rate != nil else { return }
        for (i, v) in values.enumerated() {
            guard let x = v else { reset("missing_values"); continue }
            samplesTotal += 1
            if clipped[i] { clippedTotal += 1 }
            let a = alpha
            let m = ema.map { a * $0 + (1 - a) * x } ?? x      // initialised to the first sample
            ema = m
            let y = filter!.step(x - m, channel: 0)
            since += 1
            if since > nSettle {
                tail.append(y)
                tailClip.append(clipped[i])
                if tail.count > nEnv { tail.removeFirst(tail.count - nEnv); tailClip.removeFirst(tailClip.count - nEnv) }
            }
        }
    }

    var state: EnvelopeState {
        let base = (samplesTotal, clippedTotal)
        if configError != nil { return EnvelopeState(status: "unavailable", reasons: ["filter_config_invalid"], value: nil, samplesTotal: base.0, clippedTotal: base.1) }
        if rate == nil || (since == 0 && (resetReason == "no_data" || resetReason == "configured")) {
            return EnvelopeState(status: "unavailable", reasons: ["no_data"], value: nil, samplesTotal: base.0, clippedTotal: base.1)
        }
        if tail.count < nEnv {
            if since == 0 { return EnvelopeState(status: "unavailable", reasons: [resetReason], value: nil, samplesTotal: base.0, clippedTotal: base.1) }
            var r = ["warming_up"]
            if resetReason != "no_data" && resetReason != "configured" { r.append(resetReason) }
            return EnvelopeState(status: "warming_up", reasons: r, value: nil, samplesTotal: base.0, clippedTotal: base.1)
        }
        let rms = (tail.reduce(0) { $0 + $1 * $1 } / Double(tail.count)).squareRoot()
        let clipped = tailClip.contains(true)
        return EnvelopeState(status: clipped ? "degraded" : "ok", reasons: clipped ? ["clipped"] : [],
                             value: NP.round(rms, 4), samplesTotal: base.0, clippedTotal: base.1)
    }
}

/// Relaxed/squeeze calibration and normalisation. Port of processing/calibration.py (state machine,
/// medians, acceptance rule). Normalised activation = (envelope - relaxed) / (squeeze - relaxed).
struct GripCalibration: Codable, Equatable {
    var deviceID: String
    var bootID: String
    var relaxed: Double
    var squeeze: Double
    var createdAt: Date
}

struct Normalized {
    var status: String               // ok | degraded | warming_up | unavailable
    var reasons: [String]
    var value: Double?               // unbounded
    var display: Double? { value.map { min(1, max(0, $0)) } }
}

final class GripCalibrator {
    // CalibrationConfig defaults, with the live allowance for brief peak clipping (see bridge.live_config)
    var relaxedS = 3.0, squeezeS = 3.0, discardS = 0.5
    var minUpdates = 10, maxClippedFraction = 0.10, minRatio = 2.0, minSeparation = 1.0

    private(set) var state = "uncalibrated"
    private(set) var reason: String?
    private(set) var active: GripCalibration?
    private var relaxedRef: (value: Double, boot: String)?
    private struct Capture { var phase: String; var boot: String; var tFirst: Double?; var lastT: Double?
                             var samples0 = 0; var clipped0 = 0; var values: [Double] = [] }
    private var cap: Capture?
    var events: [EngineEvent] = []

    var capturing: String? { cap?.phase }
    var captureLeftS: Double? {
        guard let c = cap else { return nil }
        let dur = c.phase == "relaxed" ? relaxedS : squeezeS
        guard let t0 = c.tFirst, let t = c.lastT else { return dur }
        return max(0, dur - (t - t0) / 1e6)
    }
    var captureDurationS: Double { cap?.phase == "squeeze" ? squeezeS : relaxedS }

    func start(_ phase: String, boot: String?) {
        guard let boot else { return reject(phase, "emg_not_configured") }
        if phase == "squeeze" {
            guard state == "relaxed_captured", let r = relaxedRef else { return reject(phase, "relaxed_reference_missing") }
            guard r.boot == boot else { return reject(phase, "relaxed_reference_incompatible") }
        } else {
            active = nil
            relaxedRef = nil
        }
        cap = Capture(phase: phase, boot: boot)
        set("capturing_\(phase)", nil)
    }

    func cancel() { if cap != nil { fail("cancelled") } }

    func install(_ c: GripCalibration) {
        active = c
        cap = nil
        relaxedRef = nil
        set("calibrated", "restored")
    }

    func onSignalReset(_ reason: String) {
        if reason == "no_data" || reason == "configured" { return }
        if let c = cap, c.tFirst != nil { fail("interrupted_\(reason)") }
    }

    func onBootChange() {
        if cap != nil { fail("interrupted_boot_change") }
        invalidate("boot_change")          // the electrodes may have moved: recalibrate after a reboot
    }

    func invalidate(_ why: String) {
        if cap != nil { fail("interrupted_\(why)") }
        if active != nil {
            active = nil
            relaxedRef = nil
            set("invalidated", why)
        } else if relaxedRef != nil {
            relaxedRef = nil
            if state == "relaxed_captured" { set("invalidated", why) }
        }
    }

    func observe(_ env: EnvelopeState, t: Double?, boot: String?) {
        guard var c = cap else { return }
        if boot != c.boot { fail("interrupted_processing_change"); return }
        guard env.status == "ok" || env.status == "degraded", let t, let v = env.value else {
            if c.tFirst != nil { fail("interrupted_" + (env.reasons.last ?? env.status)) }
            return
        }
        if let last = c.lastT, t <= last { return }
        if c.tFirst == nil { c.tFirst = t; c.samples0 = env.samplesTotal; c.clipped0 = env.clippedTotal }
        c.lastT = t
        let elapsed = (t - c.tFirst!) / 1e6
        if elapsed >= discardS { c.values.append(v) }
        cap = c
        if elapsed >= (c.phase == "relaxed" ? relaxedS : squeezeS) { finish(env) }
    }

    func normalized(_ env: EnvelopeState) -> Normalized {
        guard let a = active else {
            var r = [state != "calibrated" ? state : "uncalibrated"]
            if let reason, state == "failed" || state == "invalidated" { r.append(reason) }
            return Normalized(status: "unavailable", reasons: r, value: nil)
        }
        guard env.status == "ok" || env.status == "degraded", let v = env.value else {
            return Normalized(status: env.status == "warming_up" ? "warming_up" : "unavailable", reasons: env.reasons, value: nil)
        }
        return Normalized(status: env.status, reasons: env.reasons, value: NP.round((v - a.relaxed) / (a.squeeze - a.relaxed), 4))
    }

    private func finish(_ env: EnvelopeState) {
        guard let c = cap else { return }
        let samples = env.samplesTotal - c.samples0, clipped = env.clippedTotal - c.clipped0
        let frac = samples > 0 ? Double(clipped) / Double(samples) : 1
        if c.values.count < minUpdates { return fail("insufficient_data") }
        if frac > maxClippedFraction { return fail("clipped", detail: ["clipped_fraction": NP.round(frac, 6)]) }
        let med = NP.round(NP.median(c.values), 4)
        cap = nil
        if c.phase == "relaxed" {
            relaxedRef = (med, c.boot)
            set("relaxed_captured", nil, detail: ["relaxed": med])
            return
        }
        let rel = relaxedRef!.value
        guard med >= minRatio * rel, med - rel >= minSeparation else {
            relaxedRef = nil
            return fail("insufficient_separation", detail: ["relaxed": rel, "squeeze": med])
        }
        active = GripCalibration(deviceID: "", bootID: c.boot, relaxed: rel, squeeze: med, createdAt: Date())
        relaxedRef = nil
        set("calibrated", nil, detail: ["relaxed": rel, "squeeze": med])
    }

    private func fail(_ why: String, detail: [String: Double] = [:]) {
        cap = nil
        relaxedRef = nil
        set("failed", why, detail: detail)
    }

    private func reject(_ phase: String, _ why: String) {
        events.append(.calibrationRejected(phase: phase, reason: why))
    }

    private func set(_ s: String, _ r: String?, detail: [String: Double] = [:]) {
        state = s
        reason = r
        events.append(.calibration(state: s, reason: r, detail: detail))
    }
}
