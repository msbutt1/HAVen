import Foundation

/// Device telemetry contract v1 (signals/contract/TELEMETRY_CONTRACT.md), the records the sleeve sends.
/// Only what the processor uses is decoded; a line that does not parse is rejected whole, never repaired.
struct StreamDecl {
    var nominalRateHz: Double?
    var rawLimits: (min: Double, max: Double)?
}

struct Batch {
    var stream: String               // "emg" | "imu"
    var sampleRateHz: Double
    var firstSampleIndex: Int
    var times: [Double]              // device time of each row, microseconds
    var timingHz: Double?
    var channels: [String]
    var units: [String: String]
    var samples: [[Double?]]
    var flags: [String]
    var droppedBefore: Int
}

enum RecordBody {
    case hello(streams: [String: StreamDecl], firmware: String?)
    case status(sensors: [String: String])
    case batch(Batch)
}

struct DeviceRecord {
    var deviceID: String
    var bootID: String
    var seq: Int
    var source: String               // provenance.source: live | simulated | replay
    var body: RecordBody
}

enum RecordParser {
    static let schema = "vibeguard.device_telemetry"
    static let maxSamplesPerBatch = 256

    static func parse(_ line: Data) -> DeviceRecord? {
        guard let o = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { return nil }
        return parse(o)
    }

    static func parse(_ o: [String: Any]) -> DeviceRecord? {
        guard o["schema"] as? String == schema, num(o["version"]) == 1,
              let type = o["type"] as? String, let dev = o["device_id"] as? String, !dev.isEmpty,
              let boot = o["boot_id"] as? String, !boot.isEmpty, let seq = num(o["seq"]).map(Int.init),
              let prov = o["provenance"] as? [String: Any], let source = prov["source"] as? String,
              ["live", "simulated", "replay"].contains(source) else { return nil }
        let body: RecordBody
        switch type {
        case "hello":
            var streams: [String: StreamDecl] = [:]
            for (name, v) in (o["streams"] as? [String: Any]) ?? [:] {
                let d = v as? [String: Any] ?? [:]
                var decl = StreamDecl(nominalRateHz: num(d["nominal_rate_hz"]))
                if let l = d["raw_limits"] as? [String: Any], let lo = num(l["min"]), let hi = num(l["max"]), lo < hi {
                    decl.rawLimits = (lo, hi)
                }
                streams[name] = decl
            }
            let fw = (o["firmware"] as? [String: Any])?["version"] as? String
            body = .hello(streams: streams, firmware: fw)
        case "status":
            var sensors: [String: String] = [:]
            for (k, v) in (o["sensors"] as? [String: Any]) ?? [:] { if let s = v as? String { sensors[k] = s } }
            body = .status(sensors: sensors)
        case "emg_batch", "imu_batch":
            guard let b = batch(o, stream: type == "emg_batch" ? "emg" : "imu") else { return nil }
            body = .batch(b)
        default:
            return nil
        }
        return DeviceRecord(deviceID: dev, bootID: boot, seq: seq, source: source, body: body)
    }

    private static func batch(_ o: [String: Any], stream: String) -> Batch? {
        guard let rate = num(o["sample_rate_hz"]), rate > 0, let first = num(o["first_sample_index"]),
              let channels = o["channels"] as? [String], !channels.isEmpty,
              let units = o["units"] as? [String: String], Set(units.keys) == Set(channels),
              let rows = o["samples"] as? [[Any]], !rows.isEmpty, rows.count <= maxSamplesPerBatch,
              let timing = o["timing"] as? [String: Any], let mode = timing["mode"] as? String else { return nil }
        var samples: [[Double?]] = []
        samples.reserveCapacity(rows.count)
        for r in rows {
            guard r.count == channels.count else { return nil }
            samples.append(r.map { $0 is NSNull ? nil : num($0) })
        }
        let n = rows.count
        var times: [Double]
        var timingHz: Double?
        if mode == "uniform" {
            guard let t0 = num(timing["t0_us"]), let dt = num(timing["interval_us"]), dt > 0 else { return nil }
            times = (0..<n).map { t0 + Double($0) * dt }
            timingHz = 1e6 / dt
        } else if mode == "explicit", let ts = timing["t_us"] as? [Any], ts.count == n {
            times = ts.compactMap { num($0) }
            guard times.count == n else { return nil }
            timingHz = n >= 2 && times[n - 1] > times[0] ? Double(n - 1) * 1e6 / (times[n - 1] - times[0]) : nil
        } else {
            return nil
        }
        let q = o["quality"] as? [String: Any] ?? [:]
        return Batch(stream: stream, sampleRateHz: rate, firstSampleIndex: Int(first), times: times,
                     timingHz: timingHz, channels: channels, units: units, samples: samples,
                     flags: q["flags"] as? [String] ?? [], droppedBefore: num(q["dropped_samples_before"]).map(Int.init) ?? 0)
    }

    static func num(_ v: Any?) -> Double? {
        if let n = v as? NSNumber {
            let d = n.doubleValue
            return d.isFinite ? d : nil
        }
        if let d = v as? Double { return d.isFinite ? d : nil }
        if let i = v as? Int { return Double(i) }
        return nil
    }
}
