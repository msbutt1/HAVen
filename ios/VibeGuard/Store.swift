import Foundation

/// What the app keeps on the phone between launches: the grip calibration (used again only for the same sleeve
/// and boot) and the tool calibration.
enum Store {
    static func saveGrip(_ c: GripCalibration?) { save(c, "vg.gripCalibration") }
    static func loadGrip() -> GripCalibration? { load("vg.gripCalibration") }
    static func saveTool(_ r: ToolCalibrationResult?) { save(r, "vg.toolCalibration") }
    static func loadTool() -> ToolCalibrationResult? { load("vg.toolCalibration") }

    private static func save<T: Encodable>(_ v: T?, _ key: String) {
        if let v, let d = try? JSONEncoder().encode(v) { UserDefaults.standard.set(d, forKey: key) }
        else { UserDefaults.standard.removeObject(forKey: key) }
    }
    private static func load<T: Decodable>(_ key: String) -> T? {
        UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(T.self, from: $0) }
    }
}
