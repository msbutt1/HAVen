import SwiftUI

/// The one working screen: exposure this shift (the bar), what is happening right now, and calibration.
struct DashboardView: View {
    @Environment(AppModel.self) private var backend
    @State private var showCalibration = false

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { context in
            let now = context.date
            let fresh = backend.freshness(at: now)
            let v = fresh == .fresh ? backend.latest : nil
            VStack(alignment: .leading, spacing: 16) {
                FreshnessLine(freshness: fresh, now: now)

                ExposureCard(fresh: fresh == .fresh)

                ToolCard(tool: v?.tool, calibrated: backend.toolCalibrated)

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12)], spacing: 12) {
                    ReadoutCard(title: "Grip effort", value: v?.activationPct, digits: 0, unit: "% of max squeeze",
                                stale: fresh != .fresh,
                                note: backend.snapshot.calibrationState == "calibrated"
                                    ? "Muscle activity relative to your calibrated squeeze. Not force."
                                    : "Not calibrated. Calibrate to see grip effort.") {
                        ProgressView(value: min(100, max(0, v?.activationPct ?? 0)), total: 100).tint(Theme.activation)
                    }
                    ReadoutCard(title: "Vibration", value: v?.vibration, digits: 2, unit: "m/s²",
                                stale: fresh != .fresh,
                                note: v?.imuClipped == true ? "The motion sensor clipped: vibration is at least this, possibly more."
                                    : "Measured at the forearm, unweighted. Not a standards-grade measurement.") { EmptyView() }
                }

                CalibrationStatusCard { showCalibration = true }

                if backend.source == .demo { DemoControls() }
            }
        }
        .sheet(isPresented: $showCalibration) {
            NavigationStack {
                ScrollView { CalibrateView().padding(16) }
                    .background(Theme.background)
                    .navigationTitle("Calibrate")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showCalibration = false } } }
            }
        }
    }
}

/// Exposure this shift against the team reference, as a bar.
struct ExposureCard: View {
    @Environment(AppModel.self) private var backend
    let fresh: Bool

    var body: some View {
        let s = backend.snapshot
        let frac = backend.exposureFraction
        let color = frac >= 1 ? Theme.destructive : frac >= 0.5 ? Theme.warning : Theme.success
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Text("Exposure this shift").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
                ExperimentalTag()
                Spacer()
                Button("New shift") { backend.newShift() }.font(.footnote)
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(String(format: "%.0f", frac * 100))
                    .font(.system(size: 56, weight: .semibold)).monospacedDigit().tracking(-1.5)
                    .contentTransition(.numericText())
                Text("% of the shift reference").font(.subheadline).foregroundStyle(Theme.muted)
            }
            ExposureBar(fraction: frac, color: color)
            HStack {
                Text("\(formatScore(s.scoreTotal)) of \(formatNumber(backend.exposureReference, 0)) au")
                Spacer()
                Text(fresh && s.scoreRate != nil ? "now \(formatScore(s.scoreRate)) au/h" : "paused")
            }
            .font(.subheadline.monospacedDigit())
            Text(pausedText(s)).font(.footnote).foregroundStyle(Theme.muted)
        }
        .panel(dashed: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Exposure this shift, \(Int(frac * 100)) percent of the shift reference")
    }

    private func pausedText(_ s: EngineSnapshot) -> String {
        let since = "Since \(backend.shiftStartedAt.formatted(date: .omitted, time: .shortened))."
        guard let why = s.scorePausedReason else {
            return "\(since) Counting: vibration weighted by grip effort. The reference is a team setting, not a legal limit."
        }
        let text: String
        if why.hasPrefix("activation_uncalibrated") || why == "activation_no_data" { text = "Paused: calibrate grip to start counting." }
        else if why == "activation_stale" { text = "Paused: no recent muscle signal." }
        else if why.hasPrefix("vibration_warming_up") { text = "Starting: the motion sensor is warming up." }
        else if why.hasPrefix("vibration") { text = "Paused: no usable motion data." }
        else if why == "boot_change" { text = "Paused: the sleeve restarted." }
        else { text = "Paused: waiting for data." }
        return "\(since) \(text) Paused time is never counted."
    }
}

struct ExposureBar: View {
    let fraction: Double
    let color: Color

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.section).overlay(Capsule().strokeBorder(Theme.border))
                Capsule().fill(color)
                    .frame(width: max(0, min(1, fraction)) * g.size.width)
                    .animation(.easeOut(duration: 0.4), value: fraction)
                // 50 % mark (the bar is full at 100 %)
                ForEach([0.5], id: \.self) { m in
                    Rectangle().fill(Theme.foreground.opacity(0.35)).frame(width: 1.5).offset(x: m * g.size.width)
                }
            }
        }
        .frame(height: 22)
        .accessibilityHidden(true)
    }
}

struct CalibrationStatusCard: View {
    @Environment(AppModel.self) private var backend
    let open: () -> Void

    var body: some View {
        let grip = backend.snapshot.calibrationState == "calibrated"
        let tool = backend.toolCalibrated
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Calibration").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
                row(grip, grip ? "Grip calibrated" : "Grip not calibrated (needed for the exposure score)")
                row(tool, tool ? "Tool calibrated for this tool" : "Tool detection on default thresholds")
            }
            Spacer()
            Button(grip && tool ? "Recalibrate" : "Calibrate", action: open)
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!backend.connected)
        }
        .panel()
    }

    private func row(_ ok: Bool, _ text: String) -> some View {
        Label(text, systemImage: ok ? "checkmark.circle.fill" : "circle")
            .font(.subheadline)
            .foregroundStyle(ok ? Theme.success : Theme.foreground)
    }
}
