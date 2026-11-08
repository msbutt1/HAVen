import SwiftUI

struct SettingsView: View {
    @Environment(Backend.self) private var backend
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ConnectionSettings.load()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://localhost:8000", text: $draft.baseURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("wearable", text: $draft.runtime)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Backend")
                } footer: {
                    Text("In the Simulator the Mac is localhost. On an iPhone, use the Mac’s address on the same Wi-Fi (for example http://192.168.1.20:8000) with the backend started with --host 0.0.0.0 and a password, or the backend’s https address.")
                }
                Section {
                    TextField("Username", text: $draft.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Password", text: $draft.password)
                } header: {
                    Text("Password (if the backend has one)")
                } footer: {
                    Text("The backend’s VG_BASIC_AUTH. The password is kept in this device’s Keychain.")
                }
                Section {
                    Text("The sleeve plugs into the computer running the backend; this app shows what that backend reports. Recordings stay on that computer. VibeGuard is a prototype, not a medical device, and the exposure score is experimental, not a dose.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Connect") {
                        backend.apply(draft)
                        dismiss()
                    }
                    .disabled(draft.url(path: "/") == nil)
                }
            }
        }
    }
}
