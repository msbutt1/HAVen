import Foundation
import Security

/// Where the backend is. In the Simulator the Mac is `localhost`; a real iPhone needs the Mac's address on the
/// same Wi-Fi (the backend started with `--host 0.0.0.0` and a password), or the public https address.
struct ConnectionSettings: Equatable {
    var baseURL: String
    var runtime: String
    var username: String
    var password: String

    static let defaultURL = "http://localhost:8000"

    static func load() -> ConnectionSettings {
        let d = UserDefaults.standard
        return ConnectionSettings(baseURL: d.string(forKey: "vg.baseURL") ?? defaultURL,
                                  runtime: d.string(forKey: "vg.runtime") ?? "wearable",
                                  username: d.string(forKey: "vg.username") ?? "",
                                  password: Keychain.read("vg.password") ?? "")
    }

    func save() {
        let d = UserDefaults.standard
        d.set(baseURL, forKey: "vg.baseURL")
        d.set(runtime, forKey: "vg.runtime")
        d.set(username, forKey: "vg.username")
        if password.isEmpty { Keychain.delete("vg.password") } else { Keychain.write("vg.password", password) }
    }

    private var trimmedBase: String {
        var s = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        if !s.contains("://") { s = "http://" + s }
        return s
    }

    func url(path: String) -> URL? {
        guard let u = URL(string: trimmedBase + path), let scheme = u.scheme?.lowercased(),
              scheme == "http" || scheme == "https", u.host != nil else { return nil }
        return u
    }

    var webSocketURL: URL? {
        guard let http = url(path: "/ws/live"), var c = URLComponents(url: http, resolvingAgainstBaseURL: false) else { return nil }
        c.scheme = c.scheme == "https" ? "wss" : "ws"
        return c.url
    }

    /// HTTP Basic credentials, when a password protects the backend.
    var authorization: String? {
        guard !username.isEmpty || !password.isEmpty else { return nil }
        return "Basic " + Data("\(username):\(password)".utf8).base64EncodedString()
    }
}

enum Keychain {
    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "dev.vibeguard.ios",
         kSecAttrAccount as String: key]
    }

    static func read(_ key: String) -> String? {
        var q = query(key)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ key: String, _ value: String) {
        delete(key)
        var q = query(key)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }

    static func delete(_ key: String) {
        SecItemDelete(query(key) as CFDictionary)
    }
}
