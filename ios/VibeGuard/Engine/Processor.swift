import Foundation

/// What the screens read: the latest value of everything, after every record.
struct EngineSnapshot: Equatable {
    var deviceID: String?
    var bootID: String?
    var source: String?               // provenance: live | simulated | replay
    var firmware: String?
    var envelope: Double?             // EMG envelope, ADC counts RMS
    var envelopeStatus = "unavailable"
    var envelopeReasons: [String] = []
    var activation: Double?           // normalised, unbounded (0 = relaxed, 1 = calibrated squeeze)
    var activationStatus = "unavailable"
    var calibrationState = "uncalibrated"
    var calibrationReason: String?
    var calibrationLeftS: Double?
    var calibrationDurationS = 3.0
    var vibration: Double?            // m/s^2, combined RMS within the sensor bandwidth (unweighted)
    var vibrationStatus = "unavailable"
    var vibrationClipped = false
    var tool = ToolState(state: "unknown")
    var scoreRate: Double?            // au/h, experimental
    var scoreTotal = 0.0              // au since the engine started or the last reset
    var scorePausedReason: String?
    var scoredS = 0.0
    var staleStreams: Set<String> = []
    var records = 0
    var seqGaps = 0
    var missingRecords = 0
}

/// The on-phone processor: contract records in, snapshot and events out. Port of processing/processor.py
/// in host-clock mode (stale after 1 s without data, judged against the phone's clock). One per sleeve.
final class Processor {
    var staleAfterS = 1.0
    let countsPerG: Double

    private(set) var snapshot = EngineSnapshot()
    private(set) var events: [EngineEvent] = []

    let emg = EmgEnvelope()
    let grip = GripCalibrator()
    let vib: VibrationFeatures
    let tool: ToolUseDetector
    let score = ScoreEngine()

    private final class Stream {
        let name: String
        var nextIndex: Int?
        var lastT: Double?
        var lastRx: Double?
        var rate: Double?
        var channels: [String]?
        var stale = false
        var rateMismatch = false
        var deviceState = "unknown"
        init(_ n: String) { name = n }
        func fullReset() { nextIndex = nil; lastT = nil; lastRx = nil; rate = nil; channels = nil
                           stale = false; rateMismatch = false; deviceState = "unknown" }
    }
    private let streams = ["emg": Stream("emg"), "imu": Stream("imu")]
    private var declared: [String: StreamDecl] = [:]
    private var seqLast: Int?
    private var recent = Set<Int>()
    private var recentOrder: [Int] = []

    init(countsPerG: Double = 4096, tool config: ToolConfig = ToolConfig()) {
        self.countsPerG = countsPerG
        vib = VibrationFeatures(countsPerG: countsPerG)
        tool = ToolUseDetector(config: config, countsPerG: countsPerG)
        emg.onReset = { [weak self] r in self?.grip.onSignalReset(r) }
    }

    // MARK: API

    @discardableResult
    func feed(line: Data, rx: Double) -> Bool {
        guard let rec = RecordParser.parse(line) else { return false }
        feed(rec, rx: rx)
        return true
    }

    func feed(_ r: DeviceRecord, rx: Double) {
        if snapshot.deviceID == nil { snapshot.deviceID = r.deviceID }
        guard r.deviceID == snapshot.deviceID else { return }        // one processor per sleeve
        if r.bootID != snapshot.bootID {
            let previous = snapshot.bootID
            snapshot.bootID = r.bootID
            seqLast = nil; recent = []; recentOrder = []; declared = [:]
            for s in streams.values { s.fullReset() }
            emg.reset("no_data"); vib.reset("no_data"); tool.reset("no_data")
            events.append(.bootChange(previous: previous))
            if previous != nil { grip.onBootChange(); score.onBootChange(); tool.onBootChange() }
        }
        if recent.contains(r.seq) { return }
        if let l = seqLast, r.seq < l { return }
        if let l = seqLast, r.seq > l + 1 {
            snapshot.seqGaps += 1
            snapshot.missingRecords += r.seq - l - 1
            events.append(.seqGap(missing: r.seq - l - 1))
        }
        if snapshot.source != nil && snapshot.source != r.source {
            grip.invalidate("provenance_changed")
            score.resetAccumulation()                                 // never sum live and simulated data
            for s in streams.values { resetStream(s, "provenance_changed") }
        }
        snapshot.source = r.source
        recent.insert(r.seq); recentOrder.append(r.seq)
        if recentOrder.count > 256 { recent.remove(recentOrder.removeFirst()) }
        seqLast = max(seqLast ?? r.seq, r.seq)
        snapshot.records += 1

        switch r.body {
        case .hello(let s, let fw):
            declared = s
            snapshot.firmware = fw
        case .status(let sensors):
            for (name, state) in sensors {
                guard let st = streams[name] else { continue }
                let prev = st.deviceState
                st.deviceState = state
                if state == prev || (prev == "unknown" && state == "ok") { continue }
                events.append(.sensorState(stream: name, state: state))
                if state == "disconnected" || state == "stale" {
                    resetStream(st, "device_reports_\(state)")
                    features(st, st.lastT)
                }
            }
        case .batch(let b):
            onBatch(b, rx: rx)
        }
        checkStale(now: rx)
        collect()
    }

    /// Call about every 50 ms so silence is reported as stale.
    func poll(now: Double) {
        checkStale(now: now)
        collect()
    }

    func takeEvents() -> [EngineEvent] {
        let e = events
        events = []
        return e
    }

    func gripCalibration(_ phase: String) {
        if phase == "cancel" { grip.cancel() } else { grip.start(phase, boot: emg.rate == nil ? nil : snapshot.bootID) }
        collect()
        refreshCalibration()
    }

    func toolCalibration(_ phase: String) {
        switch phase {
        case "cancel": tool.calibrationCancel()
        case "reset": tool.calibrationReset()
        default: tool.calibrationStart(phase)
        }
        collect()
    }

    func resetToolSession() { tool.resetSession() }
    func resetScore() { score.resetAccumulation(); snapshot.scoreTotal = 0; snapshot.scoredS = 0 }

    // MARK: internals

    private func resetStream(_ s: Stream, _ reason: String) {
        if s.name == "emg" { emg.reset(reason) } else { vib.reset(reason); tool.reset(reason) }
    }

    private func onBatch(_ b: Batch, rx: Double) {
        guard let st = streams[b.stream] else { return }
        let rate = declared[b.stream]?.nominalRateHz ?? b.sampleRateHz
        if st.rate != rate {
            if st.nextIndex != nil { resetStream(st, "rate_changed") }
            st.rate = rate
        }
        let tol = 0.10 * rate
        st.rateMismatch = abs(b.sampleRateHz - rate) > tol || (b.timingHz.map { abs($0 - rate) > tol } ?? false)
        if let ch = st.channels, ch != b.channels { resetStream(st, "channels_changed") }
        st.channels = b.channels
        if b.stream == "emg" { emg.configure(rate: rate) } else { vib.configure(rate: rate); tool.configure(rate: rate) }

        if let next = st.nextIndex {
            if b.firstSampleIndex < next { return }                 // overlap: ignore
            if b.firstSampleIndex > next {
                events.append(.sampleGap(stream: b.stream, missing: b.firstSampleIndex - next))
                resetStream(st, "gap")
            } else if !st.rateMismatch, let last = st.lastT {
                let dt = 1e6 / rate, step = b.times[0] - last
                if abs(step - dt) > 0.5 * dt { resetStream(st, "timing_discontinuity") }
            }
        }
        st.nextIndex = b.firstSampleIndex + b.samples.count
        st.lastT = b.times.last
        st.lastRx = rx
        if st.stale {
            st.stale = false
            events.append(.streamRecovered(stream: b.stream))
        }
        let tEnd = b.times.last
        let blocked: String? = st.rateMismatch ? "rate_mismatch"
            : (st.deviceState == "disconnected" || st.deviceState == "stale") ? "device_reports_\(st.deviceState)"
            : b.flags.contains("sensor_disconnected") ? "sensor_disconnected"
            : b.flags.contains("stale") ? "sensor_stale" : nil
        if let blocked {
            resetStream(st, blocked)
            features(st, tEnd)
            return
        }
        let limits = declared[b.stream]?.rawLimits
        let deviceClipped = b.flags.contains("clipped")
        let clipped: [Bool] = b.samples.map { row in
            deviceClipped || (limits.map { l in row.contains { v in v.map { $0 <= l.min || $0 >= l.max } ?? false } } ?? false)
        }
        if b.stream == "emg" {
            let i = b.channels.firstIndex(of: "emg0") ?? 0
            emg.process(b.samples.map { $0[i] }, clipped: clipped)
        } else {
            let idx = ["ax", "ay", "az"].compactMap { b.channels.firstIndex(of: $0) }
            guard idx.count == 3 else { return }
            let rows: [[Double]?] = b.samples.map { r in
                let v = idx.map { r[$0] }
                return v.contains { $0 == nil } ? nil : v.map { $0! }
            }
            vib.process(rows, clipped: clipped)
            tool.process(rows)
        }
        features(st, tEnd)
    }

    private func features(_ st: Stream, _ tEnd: Double?) {
        if st.name == "emg" {
            let env = emg.state
            grip.observe(env, t: tEnd, boot: snapshot.bootID)
            let n = grip.normalized(env)
            score.updateActivation(tEnd, n)
            snapshot.envelope = env.value
            snapshot.envelopeStatus = st.stale ? "unavailable" : env.status
            snapshot.envelopeReasons = env.reasons
            snapshot.activation = n.value
            snapshot.activationStatus = n.status
            refreshCalibration()
        } else {
            let v = vib.state
            score.updateVibration(tEnd, v)
            snapshot.vibration = v.combinedRms
            snapshot.vibrationStatus = v.status
            snapshot.vibrationClipped = v.reasons.contains("clipped")
            snapshot.tool = tool.update(tEnd)
            snapshot.scoreRate = score.rate
            snapshot.scoreTotal = score.cumulative
            snapshot.scorePausedReason = score.pausedReason
            snapshot.scoredS = score.validS
        }
    }

    private func refreshCalibration() {
        snapshot.calibrationState = grip.state
        snapshot.calibrationReason = grip.reason
        snapshot.calibrationLeftS = grip.captureLeftS
        snapshot.calibrationDurationS = grip.captureDurationS
    }

    private func checkStale(now: Double) {
        for st in streams.values where !st.stale {
            guard let last = st.lastRx, now - last > staleAfterS else { continue }
            st.stale = true
            snapshot.staleStreams.insert(st.name)
            events.append(.streamStale(stream: st.name))
            resetStream(st, "stale")
            features(st, st.lastT)
        }
        snapshot.staleStreams = Set(streams.values.filter(\.stale).map(\.name))
    }

    private func collect() {
        events += grip.events; grip.events = []
        events += tool.events; tool.events = []
        refreshCalibration()
        snapshot.tool.calibrationState = tool.calState
        snapshot.tool.calibrationSource = tool.calSource
        snapshot.tool.captureLeftS = tool.captureLeftS
    }
}
