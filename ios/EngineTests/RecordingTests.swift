import XCTest
@testable import VibeGuardEngine

/// The Swift engine against the Python reference processor, on REAL recordings from the sleeve
/// (board W, 2026-11-08): the same lines and calibration presses in, the same results out.
/// Shared helpers: feed a fixture through the Swift engine and compare with the Python outputs.
class RecordingBase: XCTestCase {
    struct Run {
        var got: [String: [String: Any]] = [:]       // "stream:seq" -> values
        var events: [EngineEvent] = []
        var proc: Processor
    }

    func run(_ name: String) throws -> (Run, [String: Any]) {
        let f = try fixture("\(name).json") as! [String: Any]
        let lines = f["lines"] as! [String]
        let cmds = f["commands"] as! [String: [String]]
        var r = Run(proc: Processor(countsPerG: 4096))
        var clock = 0.0
        for line in lines {
            guard let rec = RecordParser.parse(Data(line.utf8)) else { XCTFail("did not parse"); continue }
            // device-time clock, like the reference run (clock="device"): newest device time seen
            if case .batch(let b) = rec.body { clock = max(clock, (b.times.last ?? 0) / 1e6) }
            r.proc.feed(rec, rx: clock)
            let s = r.proc.snapshot
            if case .batch(let b) = rec.body {
                if b.stream == "emg" {
                    r.got["emg:\(rec.seq)"] = ["envelope": s.envelope as Any, "activation": s.activation as Any,
                                               "calibration": s.calibrationState]
                } else {
                    r.got["imu:\(rec.seq)"] = ["vibration": s.vibration as Any]
                    r.got["tool:\(rec.seq)"] = ["state": s.tool.state, "tool": s.tool.toolBand as Any,
                                                "motion": s.tool.motionBand as Any, "dominant_hz": s.tool.dominantHz as Any,
                                                "conc": s.tool.concentration as Any, "session_tool_s": s.tool.sessionToolS]
                    r.got["score:\(rec.seq)"] = ["cumulative": s.scoreTotal]
                }
            }
            for c in cmds[String(rec.seq)] ?? [] where !(line.contains("\"type\":\"hello\"")) {
                if c.hasPrefix("toolcal:") { r.proc.toolCalibration(String(c.dropFirst(8))) } else { r.proc.gripCalibration(c) }
            }
            r.events += r.proc.takeEvents()
        }
        return (r, f)
    }

    func compare(_ r: Run, _ f: [String: Any], file: StaticString = #filePath, line: UInt = #line) -> [String: (Double, Int)] {
        var worst: [String: (Double, Int)] = [:]   // field -> (max abs error, mismatches)
        var stateMismatch = 0
        var compared: [String: Int] = [:]
        for e in f["expected"] as! [[String: Any]] {
            let stream = e["stream"] as! String, seq = e["seq"] as! Int
            guard let g = r.got["\(stream):\(seq)"] else { continue }
            for key in ["envelope", "activation", "vibration", "tool", "motion", "conc", "dominant_hz", "cumulative", "session_tool_s"] {
                guard let want = e[key] as? Double else { continue }
                guard let have = g[key] as? Double else { worst[key, default: (0, 0)].1 += 1; continue }
                let err = abs(want - have)
                worst[key] = (max(worst[key]?.0 ?? 0, err), worst[key]?.1 ?? 0)
                compared[key, default: 0] += 1
            }
            if stream == "tool", e["state"] as? String != g["state"] as? String { stateMismatch += 1 }
            if stream == "emg", e["calibration"] as? String != g["calibration"] as? String { stateMismatch += 1 }
        }
        XCTAssertEqual(stateMismatch, 0, "tool or calibration state differs", file: file, line: line)
        print("values compared:", compared)
        XCTAssertGreaterThan(compared["envelope"] ?? 0, 100, file: file, line: line)
        return worst
    }

}

final class RecordingTests: RecordingBase {
    func testGripCalibrationOnRealRecording() throws {
        let (r, f) = try run("grip_calibration")
        let worst = compare(r, f)
        print("grip worst errors:", worst)
        XCTAssertLessThan(worst["envelope"]!.0, 0.01)          // counts, rounded to 1e-4 by both
        XCTAssertLessThan(worst["activation"]?.0 ?? 0, 0.001)
        XCTAssertEqual(worst["envelope"]!.1, 0)
        XCTAssertTrue(r.events.contains { if case .calibration(state: "calibrated", _, _) = $0 { return true }; return false })
    }

    func testToolCalibrationOnRealRecording() throws {
        let (r, f) = try run("tool_calibration")
        let worst = compare(r, f)
        print("tool worst errors:", worst)
        XCTAssertLessThan(worst["vibration"]!.0, 0.001)
        XCTAssertLessThan(worst["tool"]!.0, 0.002)
        XCTAssertLessThan(worst["motion"]!.0, 0.002)
        XCTAssertLessThan(worst["conc"]!.0, 0.002)
        XCTAssertEqual(worst["dominant_hz"]?.0 ?? 0, 0)
        let want = (f["events"] as! [[String: Any]]).last { $0["state"] as? String == "calibrated" }!["thresholds"] as! [String: Double]
        guard case .toolCalibration(state: "calibrated", _, let result?)? = r.events.last(where: {
            if case .toolCalibration = $0 { return true }; return false }) else { return XCTFail("not calibrated: \(r.events)") }
        for (k, v) in want { XCTAssertEqual(result.thresholds[k]!, v, accuracy: 0.002, k) }
        print("tool thresholds:", result.thresholds, "evaluation:", result.toolWindowsAccepted, result.movementWindowsAccepted, result.movementFalseBouts)
    }
}

/// Ad hoc: a fresh live capture (Fixtures/live_check.json, not committed) when present.
final class LiveCheckTests: RecordingBase {
    func testLiveCaptureIfPresent() throws {
        guard Bundle.module.url(forResource: "Fixtures/live_check.json", withExtension: nil) != nil else { throw XCTSkip("no live capture") }
        let (r, f) = try run("live_check")
        print("live worst errors:", compare(r, f))
    }
}
