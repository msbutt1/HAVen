import SwiftUI

struct RootView: View {
    @Environment(Backend.self) private var backend
    @State private var showSettings = false
    @State private var tab: Screen = .live

    enum Screen: Hashable { case live, calibrate, test, history }

    var body: some View {
        TabView(selection: $tab) {
            screen { LiveView() }
                .tabItem { Label("Live", systemImage: "waveform.path.ecg") }
                .tag(Screen.live)
            screen { CalibrateView() }
                .tabItem { Label("Calibrate", systemImage: "hand.raised") }
                .tag(Screen.calibrate)
            screen { StationView() }
                .tabItem { Label("Test", systemImage: "hand.point.up.left") }
                .tag(Screen.test)
            screen { HistoryView() }
                .tabItem { Label("History", systemImage: "clock.arrow.circlepath") }
                .tag(Screen.history)
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .overlay(alignment: .bottom) { ToastView() }
    }

    private func screen<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ConnectionHeader()
                    content()
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
                .frame(maxWidth: 900)
                .frame(maxWidth: .infinity)
            }
            .background(Theme.background)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(backend.connected || backend.connecting ? "Disconnect" : "Connect") {
                        backend.connected || backend.connecting ? backend.disconnect() : backend.connect()
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
            }
            .toolbarBackground(Theme.background, for: .navigationBar)
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

/// Always visible: where the data comes from, and the state of the sleeve and the station.
struct ConnectionHeader: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SourceBanner()
            HStack(spacing: 8) {
                LogoMark(size: 30)
                StatusChip(title: "Sleeve", state: backend.sleeve)
                StatusChip(title: "Station", state: backend.station)
                Spacer(minLength: 0)
            }
            if let message = backend.connectionMessage {
                Label(message, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Theme.destructive)
            }
            if backend.sleeve.kind == .error || backend.station.kind == .error {
                Text([backend.sleeve.kind == .error ? "Sleeve error: \(backend.sleeve.detail)" : nil,
                      backend.station.kind == .error ? "Station error: \(backend.station.detail)" : nil]
                        .compactMap { $0 }.joined(separator: " "))
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.white)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.destructive))
            }
        }
        .padding(.top, 8)
    }
}

struct SourceBanner: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        let t = backend.latest
        let text: String
        let source: Provenance?
        if backend.connected, let t {
            source = t.provenance
            switch t.provenance {
            case .live: text = "Live data from \(t.deviceID ?? "the sleeve")"
            case .replay: text = "Recorded replay\(t.recordingID.map { " of \($0)" } ?? ""). This is not live."
            case .simulated: text = "Simulated data. These are not real measurements."
            }
        } else {
            source = nil
            text = backend.connected ? "Connected. Waiting for the first reading."
                : backend.connecting ? "Connecting to \(backend.settings.baseURL)…"
                : "Not connected. Press Connect to start the data feed."
        }
        return Text(text)
            .font(.subheadline.weight(.medium))
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 9)
            .padding(.horizontal, 12)
            .foregroundStyle(source == nil ? Theme.muted : source == .simulated ? .white
                             : source == .live ? Theme.successForeground : Theme.primaryForeground)
            .background {
                let shape = RoundedRectangle(cornerRadius: 10, style: .continuous)
                switch source {
                case .some(.live): shape.fill(Theme.success)
                case .some(.replay): shape.fill(Theme.primary)
                case .some(.simulated): Stripes(width: 12).clipShape(shape)
                case .none: shape.fill(Theme.section).overlay(shape.strokeBorder(Theme.border))
                }
            }
            .accessibilityAddTraits(.isHeader)
    }
}

struct StatusChip: View {
    let title: String
    let state: DeviceState

    private var color: Color {
        switch state.kind {
        case .ready, .completed: return Theme.success
        case .calibrating, .testing: return Theme.warning
        case .recording, .error: return Theme.destructive
        case .disconnected: return Theme.muted.opacity(0.6)
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(title).foregroundStyle(Theme.muted)
            Text(state.kind.label).fontWeight(.medium)
        }
        .font(.footnote)
        .lineLimit(1)
        .padding(.horizontal, 10)
        .frame(height: 30)
        .overlay(Capsule().strokeBorder(Theme.border))
        .accessibilityElement(children: .combine)
    }
}

struct ToastView: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        if let text = backend.toast {
            Text(text)
                .font(.subheadline.weight(.medium))
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.card))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.border))
                .shadow(color: .black.opacity(0.25), radius: 10, y: 4)
                .padding(.bottom, 64)
                .padding(.horizontal, 24)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .task(id: text) {
                    try? await Task.sleep(for: .seconds(2.4))
                    if backend.toast == text { withAnimation { backend.toast = nil } }
                }
        }
    }
}
