import Foundation

/// Where data came from. Every screen shows it: live device, a recording played back, or made-up data.
enum Provenance: String, Codable {
    case live, replay, simulated

    init(_ raw: String?) { self = Provenance(rawValue: raw ?? "") ?? .simulated }

    var label: String {
        switch self {
        case .live: return "Live"
        case .replay: return "Replay"
        case .simulated: return "Simulated"
        }
    }
}

enum DeviceStateKind: String {
    case disconnected, ready, calibrating, recording, testing, completed, error

    var label: String {
        switch self {
        case .disconnected: return "Disconnected"
        case .ready: return "Ready"
        case .calibrating: return "Calibrating"
        case .recording: return "Recording"
        case .testing: return "Testing"
        case .completed: return "Completed"
        case .error: return "Error"
        }
    }
}

struct DeviceState: Equatable {
    var kind: DeviceStateKind
    var detail: String
}

/// One update for the Live screen, built from the backend's EMG features plus the latest IMU and score records.
/// Anything the backend did not provide stays nil and the screen shows a dash.
struct Telemetry: Equatable {
    var deviceID: String?
    var provenance: Provenance
    var recordingID: String?
    var emgEnvelope: Double?
    var activationPct: Double?       // 0...100, relative to this wearer's calibration
    var vibration: Double?           // m/s^2, measured and unweighted; nil until the IMU could be converted
    var scoreRate: Double?           // au/h, experimental
    var scoreTotal: Double?          // au during the current recording
    var calibrated: Bool
    var imuClipped: Bool
    var tool: ToolState? = nil       // tool use vs normal arm use (on-phone engine)
}

struct SessionRecord: Identifiable, Equatable, Codable {
    let id: String
    var deviceID: String?
    var provenance: Provenance
    var startedAt: Date?
    var endedAt: Date?
    var durationS: Double
    var endedReason: String           // "stopped" when the person pressed Stop
    var calibrated: Bool
    var scoreTotal: Double?
    var meanActivationPct: Double?
    var peakActivationPct: Double?
    var peakVibration: Double?
    var toolTimeS: Double? = nil      // tool time during the recording
}

struct TrialResult: Identifiable, Equatable, Codable {
    let id: String
    var index: Int
    var setting: Int
    var isCatch: Bool
    var response: String               // felt | not_felt | missing
    var rtMs: Int?
}

enum StationMode: String, CaseIterable, Identifiable, Codable {
    case preview, full
    var id: String { rawValue }
    var label: String { self == .preview ? "Short preview, 6 trials" : "Full session, 8 reversals" }
}

struct StaircaseSummary: Equatable, Codable {
    var reversals: [Int]
    var reversalCount: Int
    var threshold: Double?
    var catchTrials: Int
    var catchFelt: Int
    var missing: Int
}

struct StationResult: Identifiable, Equatable, Codable {
    let id: String
    var provenance: Provenance
    var mode: StationMode
    var startedAt: Date?
    var completedAt: Date?
    var trials: [TrialResult]
    var summary: StaircaseSummary
}

enum HistoryItem: Identifiable, Equatable, Codable {
    case session(SessionRecord)
    case test(StationResult)

    var id: String {
        switch self {
        case .session(let s): return "s-" + s.id
        case .test(let t): return "t-" + t.id
        }
    }
    var date: Date {
        switch self {
        case .session(let s): return s.endedAt ?? s.startedAt ?? .distantPast
        case .test(let t): return t.completedAt ?? t.startedAt ?? .distantPast
        }
    }
}

/// The perception test's sequence: a 1-up/1-down staircase with catch trials. A stand-in until Fahad's engine is
/// wired to the backend. Catch trials and missing answers never move it.
struct Staircase: Equatable {
    let mode: StationMode
    var setting: Int
    let step: Int
    let maxTrials: Int
    let catchAt: Set<Int>
    var lastDirection: Int?
    var reversals: [Int] = []

    init(mode: StationMode) {
        self.mode = mode
        let preview = mode == .preview
        setting = preview ? 150 : 180
        step = preview ? 40 : 20
        maxTrials = preview ? 6 : 40
        catchAt = preview ? [4] : [4, 11, 19, 27]
    }

    func isCatch(_ index: Int) -> Bool { catchAt.contains(index) }

    mutating func record(setting trialSetting: Int, isCatch: Bool, response: String) {
        guard !isCatch, response != "missing" else { return }
        let direction = response == "felt" ? -1 : 1
        if let last = lastDirection, last != direction { reversals.append(trialSetting) }
        lastDirection = direction
        setting = min(255, max(10, setting + direction * step))
    }

    func isDone(trialCount: Int) -> Bool {
        mode == .full ? (reversals.count >= 8 || trialCount >= maxTrials) : trialCount >= maxTrials
    }

    /// The same rule, re-derived from stored trials (History loads tests from the backend this way).
    static func summarize(_ trials: [TrialResult], mode: StationMode) -> StaircaseSummary {
        var last: Int?
        var reversals: [Int] = []
        for t in trials where !t.isCatch && t.response != "missing" {
            let direction = t.response == "felt" ? -1 : 1
            if let l = last, l != direction { reversals.append(t.setting) }
            last = direction
        }
        let used = mode == .full ? Array(reversals.suffix(6)) : reversals
        let threshold = used.isEmpty ? nil : (Double(used.reduce(0, +)) / Double(used.count) * 10).rounded() / 10
        let catches = trials.filter(\.isCatch)
        return StaircaseSummary(reversals: used, reversalCount: reversals.count, threshold: threshold,
                                catchTrials: catches.count, catchFelt: catches.filter { $0.response == "felt" }.count,
                                missing: trials.filter { $0.response == "missing" }.count)
    }
}
