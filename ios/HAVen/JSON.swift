import Foundation

/// A loosely typed JSON value. The backend's records are deep and mostly optional, so the app reads them the
/// way the web dashboard does: walk the path, and anything missing or of the wrong type is simply absent.
enum JSON: Decodable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSON].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSON].self) { self = .object(o) }
        else { self = .null }
    }

    static func parse(_ data: Data) -> JSON? {
        try? JSONDecoder().decode(JSON.self, from: data)
    }

    subscript(key: String) -> JSON {
        if case .object(let o) = self { return o[key] ?? .null }
        return .null
    }

    subscript(index: Int) -> JSON {
        if case .array(let a) = self, a.indices.contains(index) { return a[index] }
        return .null
    }

    var double: Double? {
        if case .number(let n) = self, n.isFinite { return n }
        return nil
    }
    var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }
    var bool: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }
    var array: [JSON]? {
        if case .array(let a) = self { return a }
        return nil
    }
    var object: [String: JSON]? {
        if case .object(let o) = self { return o }
        return nil
    }
    var isNull: Bool { self == .null }

    /// The value of the first channel of a per-channel object such as `{"emg0": 183.1}` (keys in sorted order).
    var firstNumber: Double? {
        guard let o = object, let key = o.keys.sorted().first else { return nil }
        return o[key]?.double
    }
}

extension String {
    var pathEscaped: String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return addingPercentEncoding(withAllowedCharacters: allowed) ?? self
    }
    var queryEscaped: String {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&=+?#")
        return addingPercentEncoding(withAllowedCharacters: allowed) ?? self
    }
}
