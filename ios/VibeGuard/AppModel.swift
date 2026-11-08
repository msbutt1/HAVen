import Foundation
import Observation
#if canImport(UIKit)
import UIKit
#endif

/// A short time series for a chart. A gap (no data, or more than a second between points) starts a new segment,
/// so the line breaks instead of drawing across missing data.
struct ChartPoint: Identifiable, Equatable {
    let id: Int
    let t: Date
    let v: Double
    let segment: Int
}

struct Series: Equatable {
    var points: [ChartPoint] = []
    private var segment = 0
    private var nextID = 0

    mutating func push(_ t: Date, _ value: Double?, window: TimeInterval) {
        if let last = points.last, t.timeIntervalSince(last.t) > 1.0 { segment += 1 }
        if let v = value, v.isFinite {
            points.append(ChartPoint(id: nextID, t: t, v: v, segment: segment))
            nextID += 1
        } else {
            segment += 1
        }
        let cutoff = t.addingTimeInterval(-window)
        if let i = points.firstIndex(where: { $0.t >= cutoff }), i > 0 { points.removeFirst(i) }
    }

    mutating func breakLine() { segment += 1 }
    mutating func clear() { points.removeAll() }
}

enum CalPhase: Equatable { case idle, relax, squeeze, done, failed }

struct CalProgress: Equatable {
    var phase: CalPhase = .idle
    var remaining: Double = 0
    var total: Double = 3
    var message: String?
}

struct CalibrationInfo: Equatable {
    var rest: Double?
    var max: Double?
    var at: Date?
}

/// Tool calibration progress: still -> normal movement -> tool on.
struct ToolCalProgress: Equatable {
    var phase: String?                 // still | movement | tool while capturing
    var remaining: Double = 0
    var total: Double = 1
    var done: Set<String> = []
    var message: String?
    var failed = false
    var result: ToolCalibrationResult?
}

enum Freshness { case none, fresh, stale }

enum DataSource: String, CaseIterable, Identifiable {
    case sleeve, demo
    var id: String { rawValue }
    var label: String { self == .sleeve ? "Sleeve (phone hotspot)" : "Simulated sleeve (demo)" }
}

/// The whole app: the link to the sleeve, the on-phone processor (VibeGuard/Engine), the grip and tool
/// calibrations, the exposure score and tool time for the shift. Everything runs on the phone: no laptop,
/// no server.
@MainActor
@Observable
final class AppModel {

    // settings
    var source: DataSource { didSet { UserDefaults.standard.set(source.rawValue, forKey: "vg.source"); restart() } }
    var countsPerG: Double { didSet { UserDefaults.standard.set(countsPerG, forKey: "vg.countsPerG"); restart() } }

    // connection
    private(set) var connected = false             // receiving from a sleeve (real or simulated)
    private(set) var connecting = false            // listening, no sleeve yet
    private(set) var running = false
    var connectionMessage: String?
    private(set) var linkDetail = ""
    private(set) var sleeve = DeviceState(kind: .disconnected, detail: "")

    // live values
    private(set) var snapshot = EngineSnapshot()
    private(set) var latest: Telemetry?
    private(set) var lastTelemetryAt: Date?
    private(set) var updatesPerSecond: Double?
    private(set) var activation = Series()
    private(set) var vibration = Series()
    private(set) var scoreRate = Series()
    private(set) var rawSignal = Series()
    private(set) var toolBand = Series()
    private(set) var events: [String] = []

    // calibration
    private(set) var calibration: CalibrationInfo?
    private(set) var cal = CalProgress()
    private(set) var toolCal = ToolCalProgress()

    // shift (exposure score and tool time since the last "New shift")
    private(set) var shiftStartedAt = Date()
    /// Team-chosen reference for the exposure bar, in au per shift. Provisional: not a regulatory limit,
    /// not A(8). 75 au = 8 h at a vibration of 2.5 m/s^2 with 50 % grip effort, by the score's own formula.
    var exposureReference: Double { didSet { UserDefaults.standard.set(exposureReference, forKey: "vg.exposureReference") } }
    var toast: String?

    let demo = DemoSleeve()
    @ObservationIgnored let link = SleeveLink()
    @ObservationIgnored private var proc: Processor
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var lastLineAt: Double?
    @ObservationIgnored private var lineCount = 0
    @ObservationIgnored private var rateWindowStart = ProcessInfo.processInfo.systemUptime
    @ObservationIgnored private var calibrating = false
    @ObservationIgnored private var shownWarnings = Set<String>()

    init() {
        let d = UserDefaults.standard
        source = DataSource(rawValue: d.string(forKey: "vg.source") ?? "") ?? .sleeve
        let cpg = d.double(forKey: "vg.countsPerG")
        countsPerG = cpg > 0 ? cpg : 4096
        proc = Processor(countsPerG: cpg > 0 ? cpg : 4096)
        let ref = d.double(forKey: "vg.exposureReference")
        exposureReference = ref > 0 ? ref : 75
        restoreToolCalibration()
        link.onLine = { [weak self] line, t in Task { @MainActor in self?.ingest(line, t) } }
        demo.onLine = { [weak self] line, t in Task { @MainActor in self?.ingest(line, t) } }
        link.onState = { [weak self] s in Task { @MainActor in self?.linkState(s) } }
    }

    // MARK: - connection

    func connect() {
        guard !running else { return }
        running = true
        connectionMessage = nil
        if source == .demo {
            demo.start()
            linkDetail = "Simulated sleeve running in the app."
        } else {
            link.start()
            linkDetail = "Waiting for the sleeve on port \(SleeveLink.port)."
        }
        connecting = true
        sleeve = DeviceState(kind: .disconnected, detail: linkDetail)
        #if canImport(UIKit)
        UIApplication.shared.isIdleTimerDisabled = true      // iOS suspends a backgrounded app: keep it open
        #endif
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(100))
                self?.tick()
            }
        }
    }

    func disconnect() {
        running = false
        tickTask?.cancel(); tickTask = nil
        link.stop(); demo.stop()
        connected = false; connecting = false
        latest = nil
        sleeve = DeviceState(kind: .disconnected, detail: "")
        #if canImport(UIKit)
        UIApplication.shared.isIdleTimerDisabled = false
        #endif
    }

    private func restart() {
        let was = running
        disconnect()
        proc = Processor(countsPerG: countsPerG)
        restoreToolCalibration()
        snapshot = EngineSnapshot()
        calibration = nil
        cal = CalProgress()
        if was { connect() }
    }

    private func linkState(_ s: SleeveLink.State) {
        guard source == .sleeve, running else { return }
        switch s {
        case .listening:
            connecting = !connected
            linkDetail = "Waiting for the sleeve on port \(SleeveLink.port). Turn on Personal Hotspot and power the sleeve."
        case .connected(let who):
            linkDetail = "Sleeve connected from \(who)."
        case .failed(let why):
            connectionMessage = why
        case .stopped:
            break
        }
    }

    // MARK: - data in

    private func ingest(_ line: Data, _ t: Double) {
        guard running else { return }
        guard proc.feed(line: line, rx: t) else { return }
        lastLineAt = t
        lineCount += 1
        if !connected {
            connected = true
            connecting = false
            note("Sleeve data arriving")
        }
        lastTelemetryAt = Date()
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        proc.poll(now: now)
        for e in proc.takeEvents() { handle(e) }
        if now - rateWindowStart >= 1 {
            let rate = Double(lineCount) / (now - rateWindowStart)
            updatesPerSecond = rate
            lineCount = 0
            rateWindowStart = now
        }
        if connected, let last = lastLineAt, now - last > 3 {
            connected = false
            connecting = source == .sleeve
            note("Sleeve stopped sending")
        }
        let s = proc.snapshot
        snapshot = s
        sleeve = sleeveState(s)
        let t = telemetry(s)
        latest = connected ? t : nil
        let date = Date()
        activation.push(date, t.activationPct, window: 30)
        vibration.push(date, t.vibration, window: 30)
        scoreRate.push(date, t.scoreRate, window: 30)
        rawSignal.push(date, t.emgEnvelope, window: 15)
        toolBand.push(date, s.tool.toolBand, window: 30)
        for w in s.tool.warnings where !shownWarnings.contains(w) {
            shownWarnings.insert(w)
            toast = Self.warningText[w] ?? w
        }
        if let left = s.calibrationLeftS, s.calibrationState.hasPrefix("capturing") {
            cal.remaining = left
            cal.total = s.calibrationDurationS
        }
        if let left = s.tool.captureLeftS, let p = toolCal.phase {
            toolCal.remaining = left
            toolCal.total = proc.tool.duration(p)
        }
    }

    private func telemetry(_ s: EngineSnapshot) -> Telemetry {
        var act: Double?
        if s.activationStatus == "ok" || s.activationStatus == "degraded", let v = s.activation {
            act = (min(1, max(0, v)) * 1000).rounded() / 10
        }
        let fresh = s.staleStreams.isEmpty
        return Telemetry(deviceID: s.deviceID, provenance: Provenance(s.source), recordingID: nil,
                         emgEnvelope: fresh ? s.envelope : nil, activationPct: fresh ? act : nil,
                         vibration: fresh && (s.vibrationStatus == "ok" || s.vibrationStatus == "degraded") ? s.vibration : nil,
                         scoreRate: s.scoreRate,
                         scoreTotal: s.scoreTotal,
                         calibrated: s.calibrationState == "calibrated",
                         imuClipped: s.vibrationClipped, tool: s.tool)
    }

    private func sleeveState(_ s: EngineSnapshot) -> DeviceState {
        if !connected { return DeviceState(kind: .disconnected, detail: linkDetail) }
        if s.calibrationState.hasPrefix("capturing") || toolCal.phase != nil { return DeviceState(kind: .calibrating, detail: "") }
        return DeviceState(kind: .ready, detail: "")
    }

    func freshness(at now: Date) -> Freshness {
        guard connected, latest != nil, let t = lastTelemetryAt else { return .none }
        return now.timeIntervalSince(t) > 1 || !snapshot.staleStreams.isEmpty ? .stale : .fresh
    }

    private func handle(_ e: EngineEvent) {
        switch e {
        case .bootChange(let prev):
            if prev != nil {
                note("The sleeve restarted. Grip calibration must be redone.")
                calibration = nil
                Store.saveGrip(nil)
            } else {
                restoreGripCalibration()
            }
        case .seqGap(let n): if n >= 5 { note("\(n) records lost (Wi-Fi)") }
        case .streamStale(let s): note("\(s.uppercased()) data paused")
        case .streamRecovered(let s): note("\(s.uppercased()) data resumed")
        case .sensorState(let s, let st): note("\(s.uppercased()) sensor \(st)")
        case .calibration(let st, let reason, _):
            note("Grip calibration: \(st.replacingOccurrences(of: "_", with: " "))" + (reason.map { " (\($0))" } ?? ""))
            if st == "calibrated", let a = proc.grip.active {
                calibration = CalibrationInfo(rest: a.relaxed, max: a.squeeze, at: Date())
                var saved = a
                saved.deviceID = snapshot.deviceID ?? ""
                Store.saveGrip(saved)
            }
            if st == "invalidated" { calibration = nil }
        case .toolCalibration(let st, let reason, let result):
            note("Tool calibration: \(st.replacingOccurrences(of: "_", with: " "))" + (reason.map { " (\($0))" } ?? ""))
            if st == "calibrated", reason != "restored", let result { Store.saveTool(result) }
            if st == "uncalibrated" { Store.saveTool(nil) }
        case .calibrationRejected(_, let reason), .toolCalibrationRejected(let reason):
            toast = Self.calibrationText[reason] ?? reason
        case .sampleGap: break
        }
    }

    private func note(_ s: String) {
        events.append("\(Date().formatted(date: .omitted, time: .standard))  \(s)")
        if events.count > 30 { events.removeFirst(events.count - 30) }
    }

    // MARK: - grip calibration

    static let calibrationText: [String: String] = [
        "insufficient_separation": "The squeeze was not clearly stronger than the relaxed reading. Squeeze harder, or check the pads, and try again.",
        "insufficient_data": "Not enough clean signal was captured. Check the electrodes and try again.",
        "clipped": "The signal hit the top of the input range too often. Squeeze a little less hard and try again.",
        "interrupted_stale": "The Wi-Fi link paused during the capture (not your electrodes). Just try again.",
        "interrupted_gap": "Data was lost during the capture (Wi-Fi). Just try again.",
        "interrupted_boot_change": "The sleeve restarted during calibration. Start again.",
        "cancelled": "Calibration cancelled.",
        "emg_not_configured": "No muscle signal yet. Wait for the sleeve's data, then start.",
        "relaxed_reference_missing": "Do the relaxed step first.",
        // tool calibration
        "movement_looks_like_tool": "Some normal movement looked like steady tool vibration. Repeat step 2 with the tool fully off, or hold the tool more firmly in step 3.",
        "tool_not_reliably_detected": "The tool was not clearly different from normal movement. Hold the running tool firmly by its handle for the whole capture.",
        "no_tool_vibration_detected": "No vibration was seen during step 3. Is the tool on, and the sensor on the same arm?",
        "no_movement_detected": "Hardly any movement during step 2. Move more: handshakes, waving, swinging.",
        "interrupted_data_gap": "Data dropped during the capture (Wi-Fi). Just try again.",
        "movement_capture_required_first": "Do step 2 (normal movement) before step 3.",
    ]

    static let warningText: [String: String] = [
        "continuous_tool_use_long": "Tool in use for over 20 minutes without a break. Take a break.",
        "session_tool_time_approaching": "Tool time this shift is approaching 2 hours.",
        "session_tool_time_high": "Tool time this shift passed 2 hours.",
    ]

    func calibrate() async {
        guard !calibrating, connected else { return }
        calibrating = true
        defer { calibrating = false }
        rawSignal.clear()
        for (phase, action, want) in [(CalPhase.relax, "relaxed", "relaxed_captured"), (.squeeze, "squeeze", "calibrated")] {
            proc.gripCalibration(action)
            for e in proc.takeEvents() { handle(e) }
            cal = CalProgress(phase: phase, remaining: 3, total: 3)
            var state = proc.snapshot.calibrationState
            for _ in 0..<150 {        // up to 15 s: the capture runs on the sleeve's clock
                try? await Task.sleep(for: .milliseconds(100))
                state = proc.snapshot.calibrationState
                if !state.hasPrefix("capturing") { break }
            }
            guard state == want else {
                let reason = proc.snapshot.calibrationReason
                cal = CalProgress(phase: .failed, message: Self.calibrationText[reason ?? ""] ?? "Calibration did not complete (\(reason ?? state)). Try again.")
                return
            }
        }
        cal = CalProgress(phase: .done)
        toast = "Calibration saved"
    }

    func cancelCalibration() { proc.gripCalibration("cancel") }

    private func restoreGripCalibration() {
        // only for the same sleeve and the same boot: after a reboot the pads may have moved
        guard let g = Store.loadGrip(), g.deviceID == snapshot.deviceID, g.bootID == snapshot.bootID else { return }
        proc.grip.install(g)
        calibration = CalibrationInfo(rest: g.relaxed, max: g.squeeze, at: g.createdAt)
    }

    // MARK: - tool calibration

    func toolCalibrate(_ phase: String) async {
        guard connected, toolCal.phase == nil else { return }
        toolCal.message = nil
        toolCal.failed = false
        if phase == "still" { toolCal.done = []; toolCal.result = nil }
        proc.toolCalibration(phase)
        for e in proc.takeEvents() { handle(e) }
        guard proc.tool.calState == "capturing_\(phase)" else { return }
        toolCal.phase = phase
        toolCal.total = proc.tool.duration(phase)
        toolCal.remaining = toolCal.total
        for _ in 0..<400 {
            try? await Task.sleep(for: .milliseconds(100))
            if !proc.tool.calState.hasPrefix("capturing") { break }
        }
        toolCal.phase = nil
        let st = proc.tool.calState
        if st == "\(phase)_captured" || st == "calibrated" {
            toolCal.done.insert(phase)
            if st == "calibrated" {
                toolCal.result = proc.tool.calResult
                toast = "Tool calibration saved"
            }
        } else {
            toolCal.failed = true
            toolCal.result = proc.tool.calResult
            if st == "failed", let reason = proc.tool.lastFailure {
                toolCal.message = Self.calibrationText[reason] ?? reason.replacingOccurrences(of: "_", with: " ")
            } else {
                toolCal.message = st == "cancelled" ? "Cancelled." : "Tool calibration did not complete. Try again."
            }
        }
    }

    func cancelToolCalibration() { proc.toolCalibration("cancel") }

    func resetToolCalibration() {
        proc.toolCalibration("reset")
        for e in proc.takeEvents() { handle(e) }
        toolCal = ToolCalProgress()
        toast = "Tool thresholds back to the defaults"
    }

    /// Exposure score and tool time start again from zero (e.g. at the start of a work shift).
    func newShift() {
        proc.resetToolSession()
        proc.resetScore()
        shownWarnings = []
        shiftStartedAt = Date()
        toast = "New shift: exposure and tool time reset"
    }

    /// This shift's exposure as a fraction of the team reference (can exceed 1).
    var exposureFraction: Double { snapshot.scoreTotal / max(exposureReference, 1e-9) }

    var toolCalibrated: Bool { snapshot.tool.calibrationSource == "calibrated" }

    private func restoreToolCalibration() {
        if let r = Store.loadTool() { proc.tool.install(r); toolCal.result = r; toolCal.done = ["still", "movement", "tool"] }
    }

}
