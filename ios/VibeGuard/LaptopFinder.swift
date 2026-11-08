import Foundation
import Network

/// One laptop that announced VibeGuard on this Wi-Fi.
struct FoundLaptop: Identifiable, Equatable {
    let id: String               // the Bonjour service name
    var baseURL: String?         // http://host:port, once resolved
    var password: Bool
    var name: String { id }
}

/// What the laptop's announcement says (backend/src/vibeguard_backend/bonjour.py). Pure, so it can be tested.
enum BonjourTXT {
    static let serviceType = "_vibeguard._tcp"

    /// password=1 when VG_BASIC_AUTH protects the laptop's API.
    static func password(_ txt: [String: String]) -> Bool { txt["password"] == "1" }

    /// http://192.168.1.5:8000 (an IPv6 address goes in brackets, without its interface suffix).
    static func baseURL(host: String, port: UInt16) -> String {
        let h = host.split(separator: "%", maxSplits: 1).first.map(String.init) ?? host
        return h.contains(":") ? "http://[\(h)]:\(port)" : "http://\(h):\(port)"
    }
}

/// Finds laptops that announce `_vibeguard._tcp` with Bonjour, the way iOS allows (the Android app's UDP broadcast
/// would need a special Apple entitlement). Needs the local-network permission, which iOS asks for the first time.
@MainActor
@Observable
final class LaptopFinder {
    private(set) var laptops: [FoundLaptop] = []
    private(set) var searching = false
    private(set) var failure: String?

    @ObservationIgnored private var browser: NWBrowser?
    @ObservationIgnored private var resolving: [String: NWConnection] = [:]
    @ObservationIgnored private var timer: Task<Void, Never>?

    /// Search for `seconds`; laptops found stay listed after the search ends.
    func start(for seconds: Double = 5) {
        stop()
        laptops = []
        failure = nil
        searching = true
        let params = NWParameters.tcp
        params.includePeerToPeer = false
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: BonjourTXT.serviceType, domain: nil), using: params)
        b.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in self?.browserState(state) }
        }
        b.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor in self?.update(results) }
        }
        browser = b
        b.start(queue: .main)
        timer = Task { [weak self] in
            try? await Task.sleep(for: .seconds(seconds))
            self?.finish()
        }
    }

    func stop() {
        timer?.cancel()
        timer = nil
        browser?.cancel()
        browser = nil
        resolving.values.forEach { $0.cancel() }
        resolving = [:]
        searching = false
    }

    private func finish() {
        browser?.cancel()
        browser = nil
        searching = false
    }

    private func browserState(_ state: NWBrowser.State) {
        if case .failed(let error) = state {
            failure = "Could not search this network (\(error.localizedDescription))."
            finish()
        } else if case .waiting(let error) = state {
            // usually: the local-network permission was declined
            failure = "Could not search this network (\(error.localizedDescription)). Allow Local Network for VibeGuard in Settings."
        }
    }

    private func update(_ results: Set<NWBrowser.Result>) {
        for r in results {
            guard case .service(let name, _, _, _) = r.endpoint else { continue }
            var txt: [String: String] = [:]
            if case .bonjour(let record) = r.metadata { txt = record.dictionary }
            if let i = laptops.firstIndex(where: { $0.id == name }) {
                laptops[i].password = BonjourTXT.password(txt)
            } else {
                laptops.append(FoundLaptop(id: name, baseURL: nil, password: BonjourTXT.password(txt)))
                resolve(name, r.endpoint)
            }
        }
    }

    /// Turn the service into an address: open a connection to it, read where it went, close it.
    private func resolve(_ name: String, _ endpoint: NWEndpoint) {
        let params = NWParameters.tcp
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options { ip.version = .v4 }
        let c = NWConnection(to: endpoint, using: params)
        resolving[name] = c
        c.stateUpdateHandler = { [weak self, weak c] state in
            guard let c else { return }
            switch state {
            case .ready:
                let remote = c.currentPath?.remoteEndpoint
                c.cancel()
                Task { @MainActor in self?.resolved(name, remote) }
            case .failed, .cancelled:
                Task { @MainActor in self?.resolving[name] = nil }
            default:
                break
            }
        }
        c.start(queue: .main)
    }

    private func resolved(_ name: String, _ remote: NWEndpoint?) {
        resolving[name] = nil
        guard case .hostPort(let host, let port) = remote, let i = laptops.firstIndex(where: { $0.id == name }) else { return }
        let h: String
        switch host {
        case .ipv4(let a): h = "\(a)"
        case .ipv6(let a): h = "\(a)"
        case .name(let n, _): h = n
        @unknown default: return
        }
        laptops[i].baseURL = BonjourTXT.baseURL(host: h, port: port.rawValue)
    }
}
