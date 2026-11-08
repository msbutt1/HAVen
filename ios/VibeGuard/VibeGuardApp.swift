import SwiftUI

@main
struct VibeGuardApp: App {
    @State private var backend = Backend(settings: ConnectionSettings.load())

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(backend)
                .tint(Theme.primary)
                // reconnect to the laptop chosen before; a fresh install starts on the connect screen instead
                .onAppear { if UserDefaults.standard.string(forKey: "vg.baseURL") != nil { backend.connect() } }
        }
    }
}
