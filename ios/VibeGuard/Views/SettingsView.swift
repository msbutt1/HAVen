import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var backend
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var model = backend
        NavigationStack {
            Form {
                Section {
                    Picker("Data from", selection: $model.source) {
                        ForEach(DataSource.allCases) { Text($0.label).tag($0) }
                    }
                } header: {
                    Text("Sleeve")
                } footer: {
                    Text(model.source == .demo
                         ? "The in-app simulated sleeve. Every screen is labelled Simulated: these are not measurements."
                         : "Turn on Personal Hotspot on this phone with the name and password flashed into the sleeve, keep this app open, then power the sleeve. It joins the hotspot and connects to this phone (port \(SleeveLink.port)) within a few seconds. If a laptop bridge (vg-bridge) is running on the same hotspot, the sleeve connects to the laptop instead: stop it.")
                }
                Section {
                    Picker("Accelerometer range", selection: $model.countsPerG) {
                        Text("±8 g (4096 counts per g)").tag(4096.0)
                        Text("±16 g (2048 counts per g)").tag(2048.0)
                    }
                } footer: {
                    Text("Must match the sleeve firmware. Board W is flashed with ±8 g (MPU-6500, 4096 LSB/g per the TDK datasheet). A wrong value scales every vibration reading.")
                }
                Section {
                    Stepper(value: $model.exposureReference, in: 10...500, step: 5) {
                        LabeledContent("Shift reference", value: "\(Int(model.exposureReference)) au")
                    }
                } header: {
                    Text("Exposure bar")
                } footer: {
                    Text("The bar is full when this shift's score reaches the reference. Default 75 au (8 h at 2.5 m/s² with 50 % grip, by the score's formula). A team setting, not a legal limit.")
                }
                Section {
                    LabeledContent("Status", value: model.linkDetail.isEmpty ? "Stopped" : model.linkDetail)
                    LabeledContent("Device", value: model.snapshot.deviceID ?? "—")
                    LabeledContent("Firmware", value: model.snapshot.firmware ?? "—")
                    LabeledContent("Records", value: "\(model.snapshot.records)")
                    LabeledContent("Lost records", value: "\(model.snapshot.missingRecords)")
                } header: {
                    Text("Connection")
                }
                Section {
                    ForEach(model.events.reversed(), id: \.self) { Text($0).font(.caption.monospaced()) }
                } header: {
                    Text("Recent events")
                }
                Section {
                    Text("Everything is computed on this phone: muscle activation from the EMG, vibration, tool use and the experimental exposure score. Recordings and calibrations stay on this phone. VibeGuard is a prototype, not a medical device. Activation is relative to your own calibration (not force, not %MVC); the exposure score is experimental (not a dose, not A(8)); tool warnings are team settings, not regulatory limits.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}
