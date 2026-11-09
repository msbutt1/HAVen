import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var backend
    @State private var showSettings = false
    @State private var tab: Screen = .dashboard

    enum Screen: Hashable { case dashboard, overview }

    var body: some View {
        TabView(selection: $tab) {
            screen("Dashboard") { DashboardView() }
                .tabItem { Label("Dashboard", systemImage: "gauge.with.dots.needle.67percent") }
                .tag(Screen.dashboard)
            screen("Exposure score") { OverviewView() }
                .tabItem { Label("Overview", systemImage: "info.circle") }
                .tag(Screen.overview)
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .overlay(alignment: .bottom) { ToastView() }
    }

    private func screen<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
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
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(backend.running ? "Stop" : "Start") {
                        backend.running ? backend.disconnect() : backend.connect()
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

/// Always visible: where the data comes from, and the state of the sleeve.
struct ConnectionHeader: View {
    @Environment(AppModel.self) private var backend

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SourceBanner()
            HStack(spacing: 8) {
                LogoMark(size: 30)
                StatusChip(title: "Sleeve", state: backend.sleeve)
                Spacer(minLength: 0)
            }
            if let message = backend.connectionMessage {
                Label(message, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Theme.destructive)
            }
        }
        .padding(.top, 8)
    }
}

struct SourceBanner: View {
    @Environment(AppModel.self) private var backend

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
                : backend.connecting ? backend.linkDetail
                : "Stopped. Press Start to read the sleeve."
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
    @Environment(AppModel.self) private var backend

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
