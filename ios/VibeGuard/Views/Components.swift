import Charts
import SwiftUI

struct LiveView: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { context in
            let now = context.date
            let fresh = backend.freshness(at: now)
            let v = fresh == .fresh ? backend.latest : nil
            VStack(alignment: .leading, spacing: 16) {
                FreshnessLine(freshness: fresh, now: now)

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 12)], spacing: 12) {
                    ReadoutCard(title: "Muscle activation", value: v?.activationPct, digits: 0, unit: "% of max squeeze",
                                stale: fresh != .fresh,
                                note: activationNote) {
                        ProgressView(value: min(100, max(0, v?.activationPct ?? 0)), total: 100)
                            .tint(Theme.activation)
                    }
                    ReadoutCard(title: "Wrist vibration", value: v?.vibration, digits: 2, unit: "m/s²",
                                stale: fresh != .fresh,
                                note: "Measured by a hobby motion sensor, unweighted. Not a standards-grade measurement.") { EmptyView() }
                    ReadoutCard(title: "Exposure score", experimental: true, value: v?.scoreRate, digits: 2, unit: "au/h",
                                stale: fresh != .fresh,
                                note: "Placeholder formula that weights vibration by grip. Not a validated dose.") {
                        HStack(spacing: 4) {
                            Text("This recording:")
                            Text(formatScore(v?.scoreTotal)).fontWeight(.semibold).monospacedDigit()
                            Text("au").foregroundStyle(Theme.muted)
                        }
                        .font(.subheadline)
                    }
                }

                if let warning = qualityWarning(v) {
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.warning.opacity(0.14)))
                }

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 12)], spacing: 12) {
                    ChartCard(title: "Muscle activation, last 30 s", series: backend.activation, color: Theme.activation,
                              now: now, window: 30, fixedMax: 100, stale: fresh != .fresh)
                    ChartCard(title: "Wrist vibration, last 30 s", series: backend.vibration, color: Theme.vibration,
                              now: now, window: 30, floorMax: 1, stale: fresh != .fresh)
                    ChartCard(title: "Exposure score rate (experimental), last 30 s", series: backend.scoreRate, color: Theme.score,
                              now: now, window: 30, floorMax: 1, stale: fresh != .fresh, dashed: true)
                }

                RecordingBar(now: now)
            }
        }
        .navigationTitle("Live")
    }

    private var activationNote: String {
        if backend.calibration != nil { return "Calibrated in this app." }
        if backend.latest?.calibrated == true { return "Calibrated before this screen opened." }
        return "Not calibrated. Run calibration to see activation."
    }

    private func qualityWarning(_ t: Telemetry?) -> String? {
        guard let t, t.imuClipped else { return nil }
        return "The motion sensor clipped: vibration is at least what is shown, possibly more."
    }
}

func formatScore(_ v: Double?) -> String {
    guard let v else { return "—" }
    return abs(v) < 1 ? String(format: "%.3f", v) : String(format: "%.1f", v)
}

func formatNumber(_ v: Double?, _ digits: Int) -> String {
    guard let v, v.isFinite else { return "—" }
    return String(format: "%.\(digits)f", v)
}

struct FreshnessLine: View {
    @Environment(Backend.self) private var backend
    let freshness: Freshness
    let now: Date

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(freshness == .fresh ? Theme.success : freshness == .stale ? Theme.destructive : Theme.muted.opacity(0.5))
                .frame(width: 8, height: 8)
            Text(text).font(.subheadline).foregroundStyle(freshness == .stale ? Theme.destructive : Theme.muted)
        }
    }

    private var text: String {
        switch freshness {
        case .none: return backend.connected ? "Connected. Waiting for the first reading." : "No data. Connect to the backend."
        case .stale:
            let age = backend.lastTelemetryAt.map { now.timeIntervalSince($0) } ?? 0
            return String(format: "No new reading for %.1f s. Values are hidden until data resumes.", age)
        case .fresh:
            return "Updating \(Int((backend.updatesPerSecond ?? 10).rounded())) times a second."
        }
    }
}

struct ReadoutCard<Extra: View>: View {
    let title: String
    var experimental = false
    let value: Double?
    let digits: Int
    let unit: String
    let stale: Bool
    let note: String
    @ViewBuilder var extra: () -> Extra

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
                if experimental { ExperimentalTag() }
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(formatNumber(value, digits))
                    .font(.system(size: 52, weight: .semibold))
                    .monospacedDigit()
                    .tracking(-1.5)
                    .foregroundStyle(stale ? Theme.muted.opacity(0.6) : Theme.foreground)
                    .contentTransition(.numericText())
                Text(unit).font(.subheadline).foregroundStyle(Theme.muted)
            }
            extra()
            Text(note).font(.footnote).foregroundStyle(Theme.muted)
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .panel(dashed: experimental)
        .accessibilityElement(children: .combine)
    }
}

struct ChartCard: View {
    let title: String
    let series: Series
    let color: Color
    let now: Date
    let window: TimeInterval
    var fixedMax: Double?
    var floorMax: Double = 1
    var stale = false
    var dashed = false

    var body: some View {
        let start = now.addingTimeInterval(-window)
        let visible = series.points.filter { $0.t >= start }
        let top = fixedMax ?? max(floorMax, (visible.map(\.v).max() ?? 0) * 1.15)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Capsule().fill(color).frame(width: 12, height: 3)
                Text(title).font(.caption).foregroundStyle(Theme.muted)
            }
            Chart(visible) { p in
                LineMark(x: .value("Time", p.t), y: .value("Value", p.v), series: .value("Segment", p.segment))
                    .foregroundStyle(stale ? Theme.muted : color)
                    .lineStyle(StrokeStyle(lineWidth: 1.8, dash: dashed ? [5, 3] : []))
                    .interpolationMethod(.linear)
            }
            .chartXScale(domain: start...now)
            .chartYScale(domain: 0...top)
            .chartXAxis(.hidden)
            .chartYAxis {
                AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { _ in
                    AxisGridLine().foregroundStyle(Theme.border)
                    AxisValueLabel().foregroundStyle(Theme.muted).font(.caption2)
                }
            }
            .frame(height: 120)
            .accessibilityLabel(title)
        }
        .panel(padding: 12)
    }
}

struct RecordingBar: View {
    @Environment(Backend.self) private var backend
    let now: Date

    var body: some View {
        let state = backend.sleeve.kind
        let recording = state == .recording
        HStack(spacing: 12) {
            Button(recording ? "Stop recording" : "Start recording") {
                Task { await backend.toggleRecording() }
            }
            .buttonStyle(PrimaryButtonStyle(role: recording ? .destructive : nil))
            .disabled(backend.busy || !(state == .ready || recording))
            Text(message(state)).font(.subheadline).foregroundStyle(Theme.muted).monospacedDigit()
        }
        .panel(padding: 12)
    }

    private func message(_ state: DeviceStateKind) -> String {
        switch state {
        case .recording:
            if let start = backend.sessionStartedAt {
                let s = Int(now.timeIntervalSince(start))
                return String(format: "Recording, %d:%02d", s / 60, s % 60)
            }
            return "Recording"
        case .ready:
            let calibrated = backend.calibration != nil || backend.latest?.calibrated == true
            return calibrated ? "Ready to record." : "Ready to record. Not calibrated, so the score stays empty."
        case .calibrating: return "Wait for calibration to finish."
        case .error: return "Clear the sleeve error before recording."
        default: return "Connect the sleeve to record."
        }
    }
}
