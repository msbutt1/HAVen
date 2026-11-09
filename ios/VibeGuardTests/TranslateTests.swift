import XCTest
@testable import VibeGuard

/// The app must show exactly what the backend reports, by the same rules as the web dashboard.
final class TranslateTests: XCTestCase {
    private func json(_ s: String) -> JSON { JSON.parse(Data(s.utf8))! }

    private let emgCalibrated = """
    {"type":"stream_features","stream":"emg","device_id":"d1","source_seq":7,"t_us":5000000,
     "provenance":{"source":"replay","recording_id":"rec-1"},"quality":{"clipped_samples":0},
     "features":{"emg_activation":{"envelope":{"value":{"emg0":183.1}},
                                   "normalized":{"status":"ok","display":{"emg0":0.5}},
                                   "calibration":{"state":"calibrated"}}}}
    """

    func testCalibratedActivationConvertedVibrationAndScore() {
        let imu = json(#"{"features":{"vibration":{"physical":{"status":"ok","combined_rms":2.5}}}}"#)
        let score = json(#"{"status":"accumulating","rate":{"value":4.2},"cumulative":{"value":10}}"#)
        let t = Translate.telemetry(emg: json(emgCalibrated), imu: imu, score: score, status: .null,
                                    sessionActive: true, scoreBase: 9.25)
        XCTAssertEqual(t.activationPct, 50)                   // 0..1 display -> percent
        XCTAssertEqual(t.vibration, 2.5)
        XCTAssertEqual(t.scoreRate, 4.2)
        XCTAssertEqual(t.scoreTotal, 0.75)                    // cumulative - value at the start of the recording
        XCTAssertEqual(t.provenance, .replay)
        XCTAssertEqual(t.recordingID, "rec-1")
        XCTAssertEqual(t.emgEnvelope, 183.1)
        XCTAssertTrue(t.calibrated)
    }

    func testNothingIsInventedBeforeCalibrationConversionOrARecording() {
        let emg = json(#"{"provenance":{"source":"simulated"},"features":{"emg_activation":{"normalized":{"status":"unavailable","display":null},"calibration":{"state":"invalidated"}}}}"#)
        let imu = json(#"{"features":{"vibration":{"physical":{"status":"warming_up","combined_rms":null}}}}"#)
        let score = json(#"{"status":"paused","rate":{"value":null},"cumulative":{"value":10}}"#)
        let t = Translate.telemetry(emg: emg, imu: imu, score: score, status: .null, sessionActive: false, scoreBase: nil)
        XCTAssertNil(t.activationPct)
        XCTAssertNil(t.vibration)
        XCTAssertNil(t.scoreRate)
        XCTAssertNil(t.scoreTotal)
        XCTAssertFalse(t.calibrated)
        XCTAssertEqual(t.provenance, .simulated)
        XCTAssertNil(t.recordingID)                           // only a replay names its recording
    }

    func testAStaleIMUGivesNoVibration() {
        let t = Translate.telemetry(emg: json(emgCalibrated), imu: nil, score: .null, status: .null, sessionActive: false, scoreBase: nil)
        XCTAssertNil(t.vibration)
    }

    func testSleeveStateMapping() {
        XCTAssertEqual(Translate.sleeveState(json(#"{"state":"ready","connection":"connected"}"#)).kind, .ready)
        XCTAssertEqual(Translate.sleeveState(json(#"{"state":"ready","connection":"connected","active_session_id":"s1"}"#)).kind, .recording)
        let off = Translate.sleeveState(json(#"{"state":"ready","connection":"disconnected","connection_detail":"unplugged"}"#))
        XCTAssertEqual(off, DeviceState(kind: .disconnected, detail: "unplugged"))
        XCTAssertEqual(Translate.sleeveState(json(#"{"state":"ready","connection":"connected","error":"boom"}"#)),
                       DeviceState(kind: .error, detail: "boom"))
        XCTAssertEqual(Translate.sleeveState(json(#"{"state":"calibrating","connection":"connected"}"#)).kind, .calibrating)
    }

    func testSessionRecordWithReadingsScoreAndPlainEndReason() {
        let row = json("""
        {"session_id":"s1","device_id":"d1","state":"completed","end_reason":"end_of_input","source":"replay",
         "started_at":"2026-11-07T20:00:00.000Z","ended_at":"2026-11-07T20:00:30.500Z","calibration_id":"c1",
         "score":{"at_start":{"cumulative_au":1},"at_end":{"cumulative_au":3.5}},
         "counters":{"readings":{"mean_activation_pct":42.5,"peak_activation_pct":98,"peak_vibration_ms2":3.1}}}
        """)
        let r = Translate.sessionRecord(row)!
        XCTAssertEqual(r.durationS, 30.5, accuracy: 0.001)
        XCTAssertEqual(r.scoreTotal, 2.5)
        XCTAssertEqual(r.meanActivationPct, 42.5)
        XCTAssertEqual(r.peakVibration, 3.1)
        XCTAssertEqual(r.endedReason, "the replayed recording reached its end")
        XCTAssertEqual(r.provenance, .replay)
    }

    func testUncalibratedSessionHasNoScoreAndUnknownReasonsAreReadable() {
        let r = Translate.sessionRecord(json(#"{"session_id":"s2","end_reason":"some_new_reason","source":"live","score":{"at_start":{"cumulative_au":1},"at_end":{"cumulative_au":3}}}"#))!
        XCTAssertNil(r.scoreTotal)
        XCTAssertEqual(r.endedReason, "some new reason")
        XCTAssertEqual(Translate.sessionRecord(json(#"{"session_id":"s3","end_reason":"stopped_by_user"}"#))!.endedReason, "stopped")
    }

    func testStationRecordRederivesTheThresholdFromStoredTrials() {
        let row = json("""
        {"station_session_id":"ss0","state":"completed","source":"simulated","protocol":{"mode":"preview"},
         "trials":[{"trial_id":"a","stimulus":{"motor_setting":150},"response":"felt","is_catch":false,"response_latency_ms":300},
                   {"trial_id":"b","stimulus":{"motor_setting":110},"response":"not_felt","is_catch":false,"response_latency_ms":410.4},
                   {"trial_id":"c","stimulus":{"motor_setting":0},"response":"missing","is_catch":true,"response_latency_ms":null}]}
        """)
        let r = Translate.stationRecord(row)!
        XCTAssertEqual(r.summary.threshold, 110)
        XCTAssertEqual(r.summary.catchTrials, 1)
        XCTAssertEqual(r.summary.missing, 1)
        XCTAssertEqual(r.trials[1].rtMs, 410)
        XCTAssertEqual(r.provenance, .simulated)
    }

    func testCalibrationFailureText() {
        XCTAssertTrue(Translate.calibrationFailure("insufficient_separation").hasPrefix("The squeeze was not clearly stronger"))
        XCTAssertEqual(Translate.calibrationFailure("interrupted_device_reports_disconnected"), "interrupted device reports disconnected")
    }

    func testJSONHelpers() {
        let j = json(#"{"a":{"emg1":2,"emg0":1},"b":[1,"x",null],"c":true}"#)
        XCTAssertEqual(j["a"].firstNumber, 1)                  // channels in sorted order
        XCTAssertEqual(j["b"][1].string, "x")
        XCTAssertTrue(j["b"][2].isNull)
        XCTAssertTrue(j["b"][9].isNull)
        XCTAssertEqual(j["c"].bool, true)
        XCTAssertTrue(j["missing"]["deeper"].isNull)
    }

    func testSettingsURLs() {
        var s = ConnectionSettings(baseURL: "localhost:8765/", runtime: "wearable", username: "", password: "")
        XCTAssertEqual(s.url(path: "/api/runtimes")?.absoluteString, "http://localhost:8765/api/runtimes")
        XCTAssertEqual(s.webSocketURL?.absoluteString, "ws://localhost:8765/ws/live")
        XCTAssertNil(s.authorization)
        s.baseURL = "https://vibeguard.example.uz"
        s.username = "demo"; s.password = "pw"
        XCTAssertEqual(s.webSocketURL?.absoluteString, "wss://vibeguard.example.uz/ws/live")
        XCTAssertEqual(s.authorization, "Basic " + Data("demo:pw".utf8).base64EncodedString())
        s.baseURL = "ftp://nope"
        XCTAssertNil(s.url(path: "/"))
    }
}

final class StaircaseTests: XCTestCase {
    func testPreviewSequenceMatchesTheWebDashboard() {
        var s = Staircase(mode: .preview)
        let answers = ["felt", "felt", "not_felt", "missing", "felt", "not_felt"]
        var settings: [Int] = []
        var trials: [TrialResult] = []
        for (i, a) in answers.enumerated() {
            let index = i + 1
            let isCatch = s.isCatch(index)
            let setting = isCatch ? 0 : s.setting
            settings.append(setting)
            trials.append(TrialResult(id: "t\(index)", index: index, setting: setting, isCatch: isCatch, response: a, rtMs: nil))
            s.record(setting: setting, isCatch: isCatch, response: a)
        }
        XCTAssertEqual(settings, [150, 110, 70, 0, 110, 70])
        XCTAssertTrue(s.isDone(trialCount: 6))
        let sum = Staircase.summarize(trials, mode: .preview)
        XCTAssertEqual(sum.reversals, [70, 110, 70])
        XCTAssertEqual(sum.threshold, 83.3)
        XCTAssertEqual(sum.catchTrials, 1)
        XCTAssertEqual(sum.missing, 1)
    }

    func testMissingAnswersAndCatchTrialsDoNotMoveTheStaircase() {
        var s = Staircase(mode: .full)
        s.record(setting: 180, isCatch: false, response: "missing")
        XCTAssertEqual(s.setting, 180)
        s.record(setting: 0, isCatch: true, response: "felt")
        XCTAssertEqual(s.setting, 180)
        s.record(setting: 180, isCatch: false, response: "not_felt")
        XCTAssertEqual(s.setting, 200)
        for _ in 0..<10 { s.record(setting: s.setting, isCatch: false, response: "not_felt") }
        XCTAssertEqual(s.setting, 255)                         // clamped to the motor's range
    }

    func testFullSessionStopsAtEightReversals() {
        var s = Staircase(mode: .full)
        var n = 0
        while !s.isDone(trialCount: n) {
            n += 1
            s.record(setting: s.setting, isCatch: false, response: n % 2 == 0 ? "felt" : "not_felt")
        }
        XCTAssertEqual(s.reversals.count, 8)
        XCTAssertLessThan(n, 40)
    }
}
