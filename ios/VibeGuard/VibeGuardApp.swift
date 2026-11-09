import SwiftUI

@main
struct VibeGuardApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .tint(Theme.primary)
                .onAppear { model.connect() }
        }
    }
}
