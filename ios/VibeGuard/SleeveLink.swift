import Foundation
import Network

/// The phone's connection to the sleeve. The sleeve (board W) joins the phone's Personal Hotspot and, when no
/// laptop bridge answers its discovery broadcast, opens a TCP connection to the hotspot gateway, which is this
/// phone, on port 8765. It then streams telemetry contract v1 as NDJSON, one record per line.
///
/// Only one sleeve is read at a time: a new connection replaces the old one (the sleeve reconnects after a
/// Wi-Fi drop and the old socket may linger). iOS suspends apps in the background, so the app keeps the
/// screen awake while connected.
final class SleeveLink {
    enum State: Equatable { case stopped, listening, connected(String), failed(String) }

    static let port: UInt16 = 8765

    var onLine: ((Data, Double) -> Void)?          // line, receive time (s, monotonic)
    var onState: ((State) -> Void)?
    var onDisconnect: (() -> Void)?

    private let queue = DispatchQueue(label: "dev.vibeguard.sleeve-link")
    private var listener: NWListener?
    private var connection: NWConnection?
    private var buffer = Data()
    private static let maxLine = 256 * 1024

    func start() {
        queue.async { [self] in
            guard listener == nil else { return }
            do {
                let params = NWParameters.tcp
                params.allowLocalEndpointReuse = true
                let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: Self.port)!)
                l.newConnectionHandler = { [weak self] c in self?.accept(c) }
                l.stateUpdateHandler = { [weak self] s in
                    switch s {
                    case .ready: self?.report(.listening)
                    case .failed(let e): self?.report(.failed("Could not listen on port \(Self.port): \(e.localizedDescription)"))
                        self?.queue.async { self?.listener?.cancel(); self?.listener = nil }
                    case .waiting(let e): self?.report(.failed("Waiting for the network: \(e.localizedDescription)"))
                    default: break
                    }
                }
                listener = l
                l.start(queue: queue)
            } catch {
                report(.failed("Could not listen on port \(Self.port): \(error.localizedDescription)"))
            }
        }
    }

    func stop() {
        queue.async { [self] in
            connection?.cancel()
            connection = nil
            listener?.cancel()
            listener = nil
            buffer.removeAll()
            report(.stopped)
        }
    }

    private func accept(_ c: NWConnection) {
        connection?.cancel()                         // newest connection wins
        connection = c
        buffer.removeAll()
        c.stateUpdateHandler = { [weak self, weak c] s in
            guard let self, let c else { return }
            switch s {
            case .ready:
                var who = "the sleeve"
                if case .hostPort(let host, _) = c.endpoint { who = "\(host)" }
                self.report(.connected(who))
            case .failed, .cancelled:
                if self.connection === c {
                    self.connection = nil
                    self.onDisconnect?()
                    self.report(self.listener == nil ? .stopped : .listening)
                }
            default: break
            }
        }
        c.start(queue: queue)
        receive(on: c)
    }

    private func receive(on c: NWConnection) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self, self.connection === c else { return }
            if let data, !data.isEmpty {
                self.buffer.append(data)
                let now = ProcessInfo.processInfo.systemUptime
                while let nl = self.buffer.firstIndex(of: 0x0A) {
                    let line = self.buffer.subdata(in: self.buffer.startIndex..<nl)
                    self.buffer.removeSubrange(self.buffer.startIndex...nl)
                    if !line.isEmpty { self.onLine?(line, now) }
                }
                if self.buffer.count > Self.maxLine { self.buffer.removeAll() }   // never a valid line: drop it
            }
            if done || error != nil { c.cancel(); return }
            self.receive(on: c)
        }
    }

    private func report(_ s: State) { onState?(s) }
}
