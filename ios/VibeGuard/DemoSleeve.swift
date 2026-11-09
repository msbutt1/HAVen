import Foundation

/// A SIMULATED sleeve inside the app, for the Simulator and for trying the app without hardware. It produces the
/// same telemetry records as the real sleeve (contract v1), labelled `simulated`, so every screen says so.
/// "Squeeze" raises the EMG activity; "Tool running" adds a steady 120 Hz vibration; "Arm moving" adds
/// irregular arm movement with knocks (what must NOT count as tool use).
final class DemoSleeve {
    var squeeze = false
    var tool = false
    var armMoving = false
    var onLine: ((Data, Double) -> Void)?

    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "dev.vibeguard.demo")
    private let boot = "b-sim-" + String(UInt32.random(in: 0...UInt32.max), radix: 16)
    private var seq = 0, emgIndex = 0, imuIndex = 0
    private var t0 = ProcessInfo.processInfo.systemUptime
    private var phase = 0.0, knockLeft = 0, rng = SystemRandomNumberGenerator()

    func start() {
        guard timer == nil else { return }
        t0 = ProcessInfo.processInfo.systemUptime
        emit(hello())
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: .milliseconds(50))
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
    }

    func stop() { timer?.cancel(); timer = nil }

    private func base(_ type: String) -> [String: Any] {
        seq += 1
        return ["schema": "vibeguard.device_telemetry", "version": 1, "type": type, "device_id": "vg-sim-demo",
                "boot_id": boot, "seq": seq, "provenance": ["source": "simulated"]]
    }

    private func hello() -> [String: Any] {
        var h = base("hello")
        h["t_us"] = 0
        h["firmware"] = ["name": "in-app simulator", "version": "demo"]
        h["streams"] = ["emg": ["nominal_rate_hz": 1000.0, "channels": ["emg0"], "units": ["emg0": "adc_counts"],
                                "raw_limits": ["min": 0, "max": 4095]],
                        "imu": ["nominal_rate_hz": 500.0, "channels": ["ax", "ay", "az"],
                                "units": ["ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"],
                                "raw_limits": ["min": -32768, "max": 32767]]]
        return h
    }

    private func gauss() -> Double {
        let u = Double.random(in: 1e-9..<1, using: &rng), v = Double.random(in: 0..<1, using: &rng)
        return (-2 * log(u)).squareRoot() * cos(2 * .pi * v)
    }

    private func tick() {
        let elapsed = ProcessInfo.processInfo.systemUptime - t0
        while Double(emgIndex) / 1000 < elapsed - 0.05 {
            var e = base("emg_batch")
            let amp = squeeze ? 160.0 : 6.0
            e["sample_rate_hz"] = 1000.0
            e["first_sample_index"] = emgIndex
            e["timing"] = ["mode": "uniform", "t0_us": Double(emgIndex) * 1000, "interval_us": 1000.0]
            e["channels"] = ["emg0"]
            e["units"] = ["emg0": "adc_counts"]
            e["samples"] = (0..<50).map { _ in [Int(max(0, min(4095, 1950 + amp * gauss())))] }
            e["quality"] = ["flags": [String]()]
            emgIndex += 50
            emit(e)
        }
        while Double(imuIndex) / 500 < elapsed - 0.05 {
            var m = base("imu_batch")
            m["sample_rate_hz"] = 500.0
            m["first_sample_index"] = imuIndex
            m["timing"] = ["mode": "uniform", "t0_us": Double(imuIndex) * 2000, "interval_us": 2000.0]
            m["channels"] = ["ax", "ay", "az"]
            m["units"] = ["ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"]
            var rows: [[Int]] = []
            for i in 0..<25 {
                let t = Double(imuIndex + i) / 500
                var ax = 3 * gauss(), ay = 3 * gauss()
                let az = 4096 + 3 * gauss()
                if armMoving {
                    ax += 3000 * sin(2 * .pi * 2.5 * t) + 1200 * sin(2 * .pi * 0.9 * t)
                    if knockLeft == 0, Double.random(in: 0..<1, using: &rng) < 0.01 { knockLeft = 10 }
                    if knockLeft > 0 { ax += Double(knockLeft) * 600 * sin(2 * .pi * 70 * t); knockLeft -= 1 }
                }
                if tool { ax += 2400 * sin(2 * .pi * 120 * t); ay += 900 * sin(2 * .pi * 240 * t) }
                rows.append([Int(ax), Int(ay), Int(az)].map { max(-32768, min(32767, $0)) })
            }
            m["samples"] = rows
            m["quality"] = ["flags": [String]()]
            imuIndex += 25
            emit(m)
        }
    }

    private func emit(_ o: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: o) else { return }
        onLine?(d, ProcessInfo.processInfo.systemUptime)
    }
}
