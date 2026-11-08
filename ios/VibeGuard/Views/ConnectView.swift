import SwiftUI

/// The connect screen, for people who may be new to phones (the Android app's, in SwiftUI). It starts on its own: the
/// laptop used last time is tried while this Wi-Fi is searched; each laptop found is one big button; help and the
/// typed address wait behind "Can't connect?".
struct ConnectView: View {
    @Environment(Backend.self) private var backend
    @Environment(\.dismiss) private var dismiss
    @State private var finder = LaptopFinder()
    @State private var showHelp = false
    @State private var typed = ""
    @State private var attempt: String?          // the address being opened now
    @State private var signIn: FoundLaptop?      // a laptop that asks for a password
    @State private var user = ""
    @State private var pass = ""
    let canClose: Bool

    /// The address saved by an earlier connection (not the built-in localhost default).
    private var lastUsed: String? {
        UserDefaults.standard.string(forKey: "vg.baseURL").flatMap { $0.isEmpty ? nil : $0 }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(spacing: 12) {
                        LogoMark(size: 40)
                        Text("VibeGuard").font(.title2.weight(.bold))
                    }
                    Text("Connect to the laptop").font(.largeTitle.weight(.bold)).fixedSize(horizontal: false, vertical: true)
                    Text("The laptop talks to the sleeve and keeps the recordings. This phone shows them over Wi-Fi. Phone and laptop must be on the same Wi-Fi.")
                        .font(.body).foregroundStyle(Theme.muted)

                    statusCard

                    Button(showHelp ? "Hide help" : "Can’t connect?") { withAnimation { showHelp.toggle() } }
                        .buttonStyle(OutlineButtonStyle(big: true))
                    if showHelp { help }

                    Text("Not a medical device. Vibration is unweighted (not ISO 5349); grip effort is relative to each wearer’s calibration, not force.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
                .padding(20)
                .frame(maxWidth: 640)
                .frame(maxWidth: .infinity)
            }
            .background(Theme.background)
            .toolbar {
                if canClose {
                    ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                }
            }
        }
        .task { finder.start() }
        .onDisappear { finder.stop() }
        .onChange(of: backend.connected) { _, now in
            if now && attempt != nil { dismiss() }
        }
        .alert("Sign in to \(signIn?.name ?? "the laptop")", isPresented: Binding(get: { signIn != nil }, set: { if !$0 { signIn = nil } })) {
            TextField("User name", text: $user).textInputAutocapitalization(.never).autocorrectionDisabled()
            SecureField("Password", text: $pass)
            Button("Sign in") { if let l = signIn, let url = l.baseURL { open(url, user: user, pass: pass) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This laptop was started with a password (VG_BASIC_AUTH). It is kept in this phone’s Keychain.")
        }
    }

    // MARK: - the status card

    @ViewBuilder private var statusCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            let (working, title, detail) = state
            HStack(spacing: 12) {
                if working { ProgressView().controlSize(.regular) }
                Text(title).font(.title3.weight(.bold)).fixedSize(horizontal: false, vertical: true)
            }
            if let detail { Text(detail).font(.body).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true) }
            ForEach(rows, id: \.url) { row in
                LaptopRow(name: row.name, detail: row.detail) { tap(row) }
                    .disabled(attempt != nil && !failed)
            }
            if !working {
                Button("Search again") { attempt = nil; finder.start() }
                    .buttonStyle(PrimaryButtonStyle(big: true))
            }
        }
        .panel(padding: 20)
        .accessibilityElement(children: .contain)
    }

    /// The last try failed (the backend keeps retrying in the background; the message stays until it connects).
    private var failed: Bool { attempt != nil && !backend.connected && backend.connectionMessage != nil }

    /// (working, title, detail)
    private var state: (Bool, String, String?) {
        if let a = attempt {
            if failed { return (false, "Could not open \(label(a))", backend.connectionMessage) }
            return (true, "Opening \(label(a))…", nil)
        }
        if backend.connecting && lastUsed != nil {
            return (true, "Opening \(label(backend.settings.baseURL))…", "The laptop you used last time.")
        }
        if finder.searching && finder.laptops.isEmpty {
            return (true, "Looking for the laptop…", "Searching this Wi-Fi for VibeGuard. This takes a few seconds.")
        }
        if !finder.laptops.isEmpty {
            return (false, finder.laptops.count == 1 ? "Found the laptop" : "Found \(finder.laptops.count) laptops",
                    "Tap it to open the dashboard.")
        }
        return (false, "No laptop found", finder.failure ?? "Check that the laptop is on and VibeGuard is running on it, and that both are on the same Wi-Fi. Then search again, or open “Can’t connect?” below.")
    }

    private struct Row { let name: String; let detail: String; let url: String; let laptop: FoundLaptop? }

    private var rows: [Row] {
        var out = finder.laptops.compactMap { l -> Row? in
            guard let url = l.baseURL else { return nil }
            return Row(name: l.name, detail: label(url) + (l.password ? " · asks for a password" : ""), url: url, laptop: l)
        }
        if let last = lastUsed, !out.contains(where: { $0.url == last }), attempt == nil || failed {
            out.append(Row(name: "Used last time", detail: label(last), url: last, laptop: nil))
        }
        return out
    }

    private func tap(_ row: Row) {
        if let l = row.laptop, l.password, l.baseURL != backend.settings.baseURL || backend.settings.authorization == nil {
            user = backend.settings.username
            pass = ""
            signIn = l
        } else {
            open(row.url)
        }
    }

    private func open(_ url: String, user: String? = nil, pass: String? = nil) {
        var s = backend.settings
        s.baseURL = url
        if let user { s.username = user }
        if let pass { s.password = pass }
        attempt = url
        backend.apply(s)
    }

    private func label(_ url: String) -> String {
        url.replacingOccurrences(of: "http://", with: "").replacingOccurrences(of: "https://", with: "")
    }

    // MARK: - help

    private var help: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Getting connected").font(.title3.weight(.bold))
            Text("1. Put the phone and the laptop on the same Wi-Fi.\n2. On the laptop, start VibeGuard for phones (the person who set up the laptop can do this):")
                .font(.body)
            Text("vg-backend serve --device wifi --host 0.0.0.0")
                .font(.callout.monospaced())
                .textSelection(.enabled)
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.section))
            Text("3. If the laptop asks whether Python may accept incoming connections, choose Allow.\n\nSome networks keep devices apart (eduroam, guest or company Wi-Fi). Then use a phone hotspot or a separate router.")
                .font(.callout).foregroundStyle(Theme.muted)

            Text("Type the laptop’s address").font(.title3.weight(.bold)).padding(.top, 8)
            Text("Shown on the laptop under Wi-Fi settings, for example 192.168.1.23. Add the port if it is not 8000.")
                .font(.callout).foregroundStyle(Theme.muted)
            TextField("192.168.1.23", text: $typed)
                .font(.title3.monospaced())
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.go)
                .onSubmit(connectTyped)
                .padding(.horizontal, 14)
                .frame(minHeight: 56)
                .background(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.border))
            Button("Connect", action: connectTyped)
                .buttonStyle(PrimaryButtonStyle(big: true))
                .disabled(typed.trimmingCharacters(in: .whitespaces).isEmpty)
            Text("A password, a different runtime and other details are in Settings (the gear on the main screens).")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
        .panel(padding: 20)
    }

    private func connectTyped() {
        guard let url = TypedAddress.baseURL(typed) else { return }
        open(url)
    }
}

/// One laptop: a big button with its name, its address and "Open".
struct LaptopRow: View {
    let name: String
    let detail: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(name).font(.title3.weight(.bold)).foregroundStyle(Theme.foreground).multilineTextAlignment(.leading)
                    Text(detail).font(.callout).foregroundStyle(Theme.muted)
                }
                Spacer(minLength: 8)
                Text("Open")
                    .font(.headline)
                    .foregroundStyle(Theme.primaryForeground)
                    .padding(.horizontal, 18)
                    .frame(minHeight: 44)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.primary))
            }
            .padding(14)
            .frame(minHeight: 76)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.background))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.primary, lineWidth: 1.2))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open \(name), \(detail)")
    }
}

/// The laptop's address as typed: "192.168.1.23", "192.168.1.23:8010", "http://laptop.local:8000/dashboard/".
/// No scheme means http and no port means 8000 (the backend's default). Same rules as the Android app.
enum TypedAddress {
    static func baseURL(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty, !s.contains(" ") else { return nil }
        let lower = s.lowercased()
        if !lower.hasPrefix("http://") && !lower.hasPrefix("https://") {
            if lower.contains("://") { return nil }
            s = "http://" + s
        }
        guard let c = URLComponents(string: s), let host = c.host, !host.isEmpty, let scheme = c.scheme?.lowercased() else { return nil }
        var port = c.port
        if port == nil && scheme == "http" { port = 8000 }
        if let p = port, p <= 0 || p > 65535 { return nil }
        let h = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        return "\(scheme)://\(h.lowercased())" + (port.map { ":\($0)" } ?? "")
    }
}
