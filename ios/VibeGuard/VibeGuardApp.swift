import SwiftUI

@main
struct VibeGuardApp: App {
    @State private var backend = Backend(settings: ConnectionSettings.load())

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(backend)
                .tint(Theme.primary)
                .onAppear { backend.connect() }
        }
    }
}
