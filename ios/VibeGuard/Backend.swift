import Foundation
import Observation

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

enum CalPhase: Equatable { case idle, relax, relaxedCaptured, squeeze, done, failed }

struct CalProgress: Equatable {
    static let captureS = 3.5   // 3 s capture + 0.5 s discarded at the start (CalibrationConfig defaults)
    var phase: CalPhase = .idle
    var remaining: Double = 0
    var total: Double = CalProgress.captureS
    var message: String?
}

struct CalibrationInfo: Equatable {
    var rest: Double?
    var max: Double?
    var at: Date?
}

struct CurrentTrial: Equatable {
    var id: String
    var index: Int
    var setting: Int
    var isCatch: Bool
    var receivedAt: Date
    var window: Double
    var answered: Bool
}

struct StationUI: Equatable {
    enum Phase: Equatable { case idle, running, complete }
    var phase: Phase = .idle
    var mode: StationMode = .preview
    var planned: Int?
    var trials: [TrialResult] = []
    var current: CurrentTrial?
    var result: StationResult?
    var message: String?
}

enum Freshness { case none, fresh, stale }

struct HTTPResult {
    let ok: Bool
    let status: Int
    let json: JSON
    let error: String?
}

/// The app's connection to the VibeGuard backend: the live WebSocket (`/ws/live`) and the HTTP API, translated
/// into what the screens show. Same rules as the web dashboard; nothing is invented, missing values stay nil.
@MainActor
@Observable
final class Backend {
    static let stationDetail = "Simulated station: no motor is driven and results are labelled simulated."

    var settings: ConnectionSettings
    private(set) var connected = false
    private(set) var connecting = false
    var connectionMessage: String?
    private(set) var sleeve = DeviceState(kind: .disconnected, detail: "")
    private(set) var station = DeviceState(kind: .disconnected, detail: "")
    private(set) var latest: Telemetry?
    private(set) var lastTelemetryAt: Date?
    private(set) var updatesPerSecond: Double?
    private(set) var activation = Series()
    private(set) var vibration = Series()
    private(set) var scoreRate = Series()
    private(set) var rawSignal = Series()
    private(set) var calibration: CalibrationInfo?
    private(set) var cal = CalProgress()
    private(set) var sessionID: String?
    private(set) var sessionStartedAt: Date?
    private(set) var busy = false
    private(set) var history: [HistoryItem] = []
    private(set) var historyLoaded = false
    var stationUI = StationUI()
    var toast: String?
    private(set) var tool: ToolUse?
    private(set) var events: [DeviceEvent] = []
    private(set) var needsRecalibration = false

    @ObservationIgnored private let urlSession: URLSession
    @ObservationIgnored private var socket: URLSessionWebSocketTask?
    @ObservationIgnored private var wantOpen = false
    @ObservationIgnored private var retryDelay: Double = 0.5
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var status: JSON = .null
    @ObservationIgnored private var imu: (record: JSON, at: Date)?
    @ObservationIgnored private var score: JSON = .null
    @ObservationIgnored private var scoreBase: Double?
    @ObservationIgnored private var announced = Set<String>()
    @ObservationIgnored private var lastEmgAt: Date?
    @ObservationIgnored private var ridCounter = 0
    @ObservationIgnored private var calibrating = false
    @ObservationIgnored private var calCancelled = false
    @ObservationIgnored private var eventCounter = 0
    @ObservationIgnored private var stationTask: Task<Void, Never>?
    @ObservationIgnored private var stationSID: String?
    @ObservationIgnored private var staircase: Staircase?
    @ObservationIgnored private var pendingAnswer: (trialID: String, response: String, rtMs: Int?)?

    init(settings: ConnectionSettings) {
        self.settings = settings
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 10
        config.waitsForConnectivity = false
        urlSession = URLSession(configuration: config)
    }

    // MARK: - connection

    func connect() {
        wantOpen = true
        connectionMessage = nil
        guard socket == nil else { return }
        open()
        if pollTask == nil {
            pollTask = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(2))
                    await self?.refresh()
                }
            }
        }
    }

    func disconnect() {
        wantOpen = false
        pollTask?.cancel()
        pollTask = nil
        let s = socket
        socket = nil
        s?.cancel(with: .goingAway, reason: nil)
        closed(message: nil)
    }

    func apply(_ new: ConnectionSettings) {
        new.save()
        settings = new
        disconnect()
        history = []
        historyLoaded = false
        announced = []
        connect()
    }

    private func open() {
        guard let url = settings.webSocketURL else {
            connectionMessage = "The backend address is not valid. Open Settings."
            return
        }
        var request = URLRequest(url: url)
        if let auth = settings.authorization { request.setValue(auth, forHTTPHeaderField: "Authorization") }
        let task = urlSession.webSocketTask(with: request)
        task.maximumMessageSize = 8 * 1024 * 1024
        socket = task
        connecting = true
        task.resume()
        Task { [weak self] in await self?.receive(on: task) }
    }

    private func receive(on task: URLSessionWebSocketTask) async {
        while true {
            do {
                let message = try await task.receive()
                guard socket === task else { return }
                if !connected {
                    connected = true
                    connecting = false
                    retryDelay = 0.5
                    connectionMessage = nil
                    Task { await self.refresh(); await self.loadHistory() }
                }
                switch message {
                case .string(let s): if let j = JSON.parse(Data(s.utf8)) { route(j) }
                case .data(let d): if let j = JSON.parse(d) { route(j) }
                @unknown default: break
                }
            } catch {
                guard socket === task else { return }
                socket = nil
                let was = connected
                closed(message: was ? "Lost connection to the backend." : nil)
                if !was { await diagnose() }
                if wantOpen { scheduleRetry() }
                return
            }
        }
    }

    private func scheduleRetry() {
        let delay = retryDelay
        retryDelay = min(retryDelay * 2, 5)
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, self.wantOpen, self.socket == nil else { return }
            self.open()
        }
    }

    /// The WebSocket failed before it opened: find out why with a plain request, to say something useful.
    private func diagnose() async {
        let r = await http("GET", "/api/runtimes")
        if r.ok {
            connectionMessage = "The backend answers but the live connection failed. Is it the VibeGuard backend?"
        } else if r.status == 401 {
            connectionMessage = "Wrong username or password. Open Settings."
        } else if r.status == 403 {
            connectionMessage = "The backend refused this address. Start it with --host 0.0.0.0 (and a password) to reach it from a phone."
        } else {
            connectionMessage = "Cannot reach \(settings.baseURL). Is the backend running, and is the address right?"
        }
    }

    private func closed(message: String?) {
        let wasConnected = connected
        connected = false
        connecting = false
        status = .null
        latest = nil
        updatesPerSecond = nil
        sleeve = DeviceState(kind: .disconnected, detail: message ?? "")
        station = DeviceState(kind: .disconnected, detail: message ?? "")
        for now in [Date()] {
            activation.push(now, nil, window: 30); vibration.push(now, nil, window: 30)
            scoreRate.push(now, nil, window: 30); rawSignal.push(now, nil, window: 15)
        }
        if stationTask != nil { stopStationLocally(reason: message ?? "Disconnected") }
        if cal.phase == .relax || cal.phase == .squeeze || cal.phase == .relaxedCaptured {
            cal = CalProgress(phase: .failed, message: "\(message ?? "Disconnected"). Calibration was not saved.")
        }
        if let message, wasConnected { connectionMessage = message; toast = message }
    }

    // MARK: - incoming

    private func route(_ m: JSON) {
        guard let channel = m["channel"].string else { return }
        if channel == "snapshot" {
            let rt = (m["payload"]["runtimes"].array ?? []).first { $0["runtime"].string == settings.runtime }
            if let rt {
                let latest = rt["latest"]
                if !latest["imu"]["features"].isNull { imu = (latest["imu"], Date()) }
                if !latest["score"]["rate"].isNull { score = latest["score"] }
                if latest["tool_use"]["type"].string == "tool_use" {
                    tool = Translate.toolUse(latest["tool_use"])
                } else if !rt["tool_use_saved"]["record"].isNull {
                    tool = Translate.toolUse(rt["tool_use_saved"]["record"], saved: true,
                                             savedAt: Translate.date(rt["tool_use_saved"]["saved_at"].string))
                }
                applyStatus(rt)
            } else {
                sleeve = DeviceState(kind: .disconnected, detail: "The backend has no runtime called \"\(settings.runtime)\".")
            }
            return
        }
        guard m["runtime"].string == settings.runtime else { return }
        let p = m["payload"]
        switch channel {
        case "derived":
            switch (p["type"].string, p["stream"].string) {
            case ("experimental_score", _): score = p
            case ("tool_use", _): tool = Translate.toolUse(p)
            case ("stream_event", _): addEvent(p)
            case ("stream_features", "imu"): imu = (p, Date())
            case ("stream_features", "emg"): telemetry(p)
            default: break
            }
        case "runtime":
            if p["runtime"].string == settings.runtime, p["state"].string != nil { applyStatus(p) } else { Task { await refresh() } }
        case "session":
            if let state = p["session"]["state"].string, state != "recording" { sessionEnded(p["session"]) }
            Task { await refresh() }
        default:
            break
        }
    }

    private func telemetry(_ p: JSON) {
        let now = Date()
        if let last = lastEmgAt {
            let dt = now.timeIntervalSince(last)
            if dt > 0 {
                let rate = 1 / dt
                updatesPerSecond = updatesPerSecond.map { $0 * 0.9 + rate * 0.1 } ?? rate
            }
        }
        lastEmgAt = now
        let freshIMU = imu.flatMap { now.timeIntervalSince($0.at) < 2 ? $0.record : nil }
        let t = Translate.telemetry(emg: p, imu: freshIMU, score: score, status: status,
                                    sessionActive: sessionID != nil, scoreBase: scoreBase)
        latest = t
        lastTelemetryAt = now
        activation.push(now, t.activationPct, window: 30)
        vibration.push(now, t.vibration, window: 30)
        scoreRate.push(now, t.scoreRate, window: 30)
        rawSignal.push(now, t.emgEnvelope, window: 15)
    }

    func freshness(at now: Date) -> Freshness {
        guard connected, latest != nil, let t = lastTelemetryAt else { return .none }
        return now.timeIntervalSince(t) > 1 ? .stale : .fresh
    }

    private func applyStatus(_ rt: JSON) {
        status = rt
        let active = rt["active_session_id"].string
        if active != sessionID {
            sessionID = active
            sessionStartedAt = active == nil ? nil : (sessionStartedAt ?? Date())
        }
        let s = Translate.sleeveState(rt)
        if s != sleeve { sleeve = s }
        if station.kind == .disconnected && stationTask == nil {
            station = DeviceState(kind: .ready, detail: Self.stationDetail)
        }
    }

    func refresh() async {
        guard connected else { return }
        let r = await http("GET", "/api/runtimes/\(settings.runtime.pathEscaped)")
        if r.ok, connected { applyStatus(r.json) }
    }

    // MARK: - HTTP

    func http(_ method: String, _ path: String, _ body: [String: Any]? = nil) async -> HTTPResult {
        guard let url = settings.url(path: path) else {
            return HTTPResult(ok: false, status: 0, json: .null, error: "The backend address is not valid.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let auth = settings.authorization { request.setValue(auth, forHTTPHeaderField: "Authorization") }
        if let body, let data = try? JSONSerialization.data(withJSONObject: body) {
            request.httpBody = data
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, response) = try await urlSession.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = JSON.parse(data) ?? .null
            if (200..<300).contains(code) { return HTTPResult(ok: true, status: code, json: json, error: nil) }
            if code == 401 { return HTTPResult(ok: false, status: code, json: json, error: "Wrong username or password.") }
            let detail = json["detail"]
            let text = detail.string
                ?? detail.array?.compactMap { $0["msg"].string }.joined(separator: "; ")
                ?? "Backend error \(code)"
            return HTTPResult(ok: false, status: code, json: json, error: text)
        } catch {
            return HTTPResult(ok: false, status: 0, json: .null, error: "Cannot reach the backend.")
        }
    }

    private func rid(_ prefix: String) -> String {
        ridCounter += 1
        return "ios-\(prefix)-\(Int(Date().timeIntervalSince1970 * 1000))-\(ridCounter)"
    }

    private var runtimePath: String { "/api/runtimes/\(settings.runtime.pathEscaped)" }

    // MARK: - recording

    func toggleRecording() async {
        busy = true
        defer { busy = false }
        if sleeve.kind == .recording { await stopRecording() } else { await startRecording() }
    }

    private func startRecording() async {
        let r = await http("POST", "/api/sessions", ["runtime": settings.runtime, "label": "iOS recording", "request_id": rid("rec")])
        guard r.ok else { toast = r.error; return }
        scoreBase = r.json["score"]["at_start"]["cumulative_au"].double
        sessionID = r.json["session_id"].string
        sessionStartedAt = Date()
        await refresh()
    }

    private func stopRecording() async {
        guard let sid = sessionID ?? status["active_session_id"].string else { toast = "No recording is running."; return }
        let r = await http("POST", "/api/sessions/\(sid.pathEscaped)/stop", ["request_id": rid("stop")])
        if r.ok {
            sessionEnded(r.json)
        } else {
            // the backend may already have ended it (the replay ended, the sleeve changed): not a failure
            let g = await http("GET", "/api/sessions/\(sid.pathEscaped)")
            guard g.ok, let state = g.json["state"].string, state != "recording" else { toast = r.error; return }
            sessionEnded(g.json)
        }
        sessionID = nil
        sessionStartedAt = nil
        await refresh()
    }

    private func sessionEnded(_ row: JSON) {
        guard let rec = Translate.sessionRecord(row), !announced.contains(rec.id) else { return }
        announced.insert(rec.id)
        history.insert(.session(rec), at: 0)
        toast = rec.endedReason == "stopped" ? "Recording saved" : "Recording ended early and was saved: \(rec.endedReason)"
    }

    // MARK: - calibration

    /// One step of the guide's calibration flow: relaxed, then squeeze. Sends the command, counts down while the
    /// backend captures, then reports the processor's state (relaxed_captured / calibrated, or failed with a reason).
    func calibrateStep(_ action: String) async {
        guard !calibrating, action == "relaxed" || action == "squeeze" else { return }
        calibrating = true
        calCancelled = false
        defer { calibrating = false; Task { await self.refresh() } }
        if action == "relaxed" { rawSignal.clear() }
        let phase: CalPhase = action == "relaxed" ? .relax : .squeeze
        let want = action == "relaxed" ? "relaxed_captured" : "calibrated"
        let r = await http("POST", "\(runtimePath)/calibration", ["action": action, "request_id": rid("cal")])
        guard r.ok else { cal = CalProgress(phase: .failed, message: r.error); return }
        let start = Date()
        while true {   // the countdown for the wearer; the backend does the real capture (3 s + 0.5 s discarded)
            if calCancelled { return }
            let left = max(0, CalProgress.captureS - Date().timeIntervalSince(start))
            cal = CalProgress(phase: phase, remaining: left)
            if left <= 0 { break }
            try? await Task.sleep(for: .milliseconds(100))
        }
        var state: JSON = .null
        for _ in 0..<30 {   // wait for the processor to finish the capture and say how it went
            if calCancelled { return }
            let s = await http("GET", runtimePath)
            if s.ok {
                applyStatus(s.json)
                state = s.json["calibration"]
                if !(state["state"].string ?? "").hasPrefix("capturing") { break }
            }
            try? await Task.sleep(for: .milliseconds(150))
        }
        guard state["state"].string == want else {
            cal = CalProgress(phase: .failed, message: Translate.calibrationFailure(state["reason"].string))
            return
        }
        if action == "relaxed" {
            cal = CalProgress(phase: .relaxedCaptured)
            return
        }
        let device = status["device_id"].string ?? ""
        let c = await http("GET", "/api/calibrations?device_id=\(device.queryEscaped)")
        let rec = c.json.array?.first
        let ref = rec?["record"]["reference"]
        calibration = CalibrationInfo(rest: ref?["relaxed"].firstNumber, max: ref?["squeeze"].firstNumber,
                                      at: Translate.date(rec?["created_at"].string) ?? Date())
        cal = CalProgress(phase: .done)
        needsRecalibration = false
        toast = "Calibration saved"
    }

    func cancelCalibration() async {
        calCancelled = true
        _ = await http("POST", "\(runtimePath)/calibration", ["action": "cancel", "request_id": rid("cal")])
        cal = CalProgress()
        await refresh()
    }

    // MARK: - tool use

    func newShift() async {
        let r = await http("POST", "\(runtimePath)/tool-use/new-shift", ["request_id": rid("shift")])
        toast = r.ok ? "New shift started: tool time reset" : r.error
    }

    private func addEvent(_ p: JSON) {
        guard let event = p["event"].string else { return }
        let detail = p["detail"]
        if event == "boot_change", !detail["previous_boot_id"].isNull {
            needsRecalibration = true    // calibration is not kept across a wearable reboot
            calibration = nil
            if cal.phase != .relax && cal.phase != .squeeze { cal = CalProgress() }
        }
        eventCounter += 1
        let warn = ["boot_change", "stream_stale", "seq_gap", "invalid_record"].contains(event)
        events.insert(DeviceEvent(id: eventCounter, event: event, text: Translate.eventText(event, detail: detail),
                                  at: Date(), isWarning: warn), at: 0)
        if events.count > 30 { events.removeLast(events.count - 30) }
    }

    // MARK: - history

    func loadHistory() async {
        let r = await http("GET", "/api/history?limit=200")
        guard r.ok, let items = r.json["items"].array else { return }
        var out: [HistoryItem] = []
        for row in items where row["kind"].string == "wearable" && row["state"].string != "recording" {
            if let rec = Translate.sessionRecord(row) {
                announced.insert(rec.id)
                out.append(.session(rec))
            }
        }
        // history rows carry only a summary: the newest finished tests are fetched with their trials
        let finished = items.filter { $0["kind"].string == "station" && $0["state"].string == "completed" }.prefix(20)
        for row in finished {
            let id = row["station_session_id"].string ?? ""
            let detail = await http("GET", "/api/station/sessions/\(id.pathEscaped)")
            if let rec = Translate.stationRecord(detail.ok ? detail.json : row) { out.append(.test(rec)) }
        }
        let known = Set(out.map(\.id))
        let local = history.filter { !known.contains($0.id) }
        history = (out + local).sorted { $0.date > $1.date }
        historyLoaded = true
    }

    // MARK: - perception test (backend station API, simulated controller)

    func startStationTest(mode: StationMode) async {
        guard stationTask == nil else { stationUI.message = "A test is already running."; return }
        let stair = Staircase(mode: mode)
        let r = await http("POST", "/api/station/sessions", [
            "source": "simulated",
            "label": "iOS \(mode.rawValue) test",
            "protocol": ["kind": "staircase_1up_1down", "mode": mode.rawValue, "start_setting": stair.setting,
                         "step": stair.step, "units": "pwm_duty_0_255",
                         "note": "stand-in sequence; not a validated procedure"],
            "request_id": rid("st"),
        ])
        guard r.ok, let sid = r.json["station_session_id"].string else {
            stationUI.message = r.error ?? "The backend did not start the test."
            return
        }
        stationSID = sid
        staircase = stair
        station = DeviceState(kind: .testing, detail: Self.stationDetail)
        stationUI = StationUI(phase: .running, mode: mode, planned: mode == .preview ? stair.maxTrials : nil)
        let startedAt = Date()
        stationTask = Task { [weak self] in await self?.runStation(sid: sid, startedAt: startedAt) }
    }

    private func runStation(sid: String, startedAt: Date) async {
        while !Task.isCancelled, var stair = staircase {
            try? await Task.sleep(for: .milliseconds(Int.random(in: 900...2200)))   // so the next trial cannot be anticipated
            if Task.isCancelled { return }
            let index = stationUI.trials.count + 1
            let isCatch = stair.isCatch(index)
            let setting = isCatch ? 0 : stair.setting
            let p = await http("POST", "/api/station/sessions/\(sid.pathEscaped)/stimulus", [
                "stimulus": ["motor_setting": setting, "units": "pwm_duty_0_255"], "is_catch": isCatch,
                "response_timeout_s": 4.0, "request_id": rid("stim"),
            ])
            if Task.isCancelled { return }
            guard p.ok, let trialID = p.json["trial_id"].string else {
                await failStation(sid: sid, reason: p.error ?? "The station refused the stimulus.")
                return
            }
            pendingAnswer = nil
            stationUI.current = CurrentTrial(id: trialID, index: index, setting: setting, isCatch: isCatch,
                                             receivedAt: Date(), window: 4, answered: false)
            let deadline = Date().addingTimeInterval(4.4)
            while Date() < deadline, pendingAnswer?.trialID != trialID, !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(50))
            }
            if Task.isCancelled { return }
            var response = "missing"
            var rt: Int?
            if let a = pendingAnswer, a.trialID == trialID {
                response = a.response
                rt = a.rtMs
            } else {   // the backend timed it out and stored it: read what it stored, never assume "not felt"
                let d = await http("GET", "/api/station/sessions/\(sid.pathEscaped)")
                let rec = d.json["trials"][index - 1]
                response = rec["response"].string ?? "missing"
                rt = rec["response_latency_ms"].double.map { Int($0.rounded()) }
            }
            let result = TrialResult(id: trialID, index: index, setting: setting, isCatch: isCatch,
                                     response: response, rtMs: response == "missing" ? nil : rt)
            stationUI.trials.append(result)
            stationUI.current = nil
            stair.record(setting: setting, isCatch: isCatch, response: response)
            staircase = stair
            if stair.isDone(trialCount: stationUI.trials.count) {
                await completeStation(sid: sid, startedAt: startedAt)
                return
            }
        }
    }

    func respond(_ response: String) async {
        guard let sid = stationSID, var current = stationUI.current, !current.answered else { return }
        current.answered = true
        stationUI.current = current
        let r = await http("POST", "/api/station/sessions/\(sid.pathEscaped)/respond", ["response": response, "request_id": rid("sr")])
        if r.ok {
            pendingAnswer = (current.id, r.json["response"].string ?? response,
                             r.json["response_latency_ms"].double.map { Int($0.rounded()) })
        } else {
            toast = r.error
        }
    }

    private func completeStation(sid: String, startedAt: Date) async {
        let r = await http("POST", "/api/station/sessions/\(sid.pathEscaped)/complete", ["request_id": rid("stc")])
        guard r.ok else { await failStation(sid: sid, reason: r.error ?? "The backend did not save the test."); return }
        let mode = stationUI.mode
        let result = StationResult(id: sid, provenance: .simulated, mode: mode, startedAt: startedAt, completedAt: Date(),
                                   trials: stationUI.trials, summary: Staircase.summarize(stationUI.trials, mode: mode))
        stationTask = nil
        stationSID = nil
        staircase = nil
        stationUI.phase = .complete
        stationUI.result = result
        history.insert(.test(result), at: 0)
        station = DeviceState(kind: .completed, detail: Self.stationDetail)
        toast = "Perception test saved"
        try? await Task.sleep(for: .milliseconds(300))
        if station.kind == .completed { station = DeviceState(kind: .ready, detail: Self.stationDetail) }
    }

    private func failStation(sid: String, reason: String) async {
        stopStationLocally(reason: reason)
        _ = await http("POST", "/api/station/sessions/\(sid.pathEscaped)/abort", ["reason": reason, "request_id": rid("sta")])
        if connected { station = DeviceState(kind: .ready, detail: Self.stationDetail) }
    }

    func abortStationTest() async {
        guard let sid = stationSID else { return }
        stopStationLocally(reason: "Aborted by operator")
        let r = await http("POST", "/api/station/sessions/\(sid.pathEscaped)/abort", ["reason": "Aborted by operator", "request_id": rid("sta")])
        if !r.ok { stationUI.message = "Test stopped, but the backend could not confirm it: \(r.error ?? "")" }
        if connected { station = DeviceState(kind: .ready, detail: Self.stationDetail) }
    }

    private func stopStationLocally(reason: String) {
        stationTask?.cancel()
        stationTask = nil
        stationSID = nil
        staircase = nil
        stationUI.phase = .idle
        stationUI.current = nil
        stationUI.message = "Test stopped: \(reason). The backend keeps it as an aborted test; it is not a result."
    }

    func resetStation() {
        stationUI = StationUI(mode: stationUI.mode)
    }
}
