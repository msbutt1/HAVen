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
                    ReadoutCard(title: "Grip effort", value: v?.activationPct.map { max(0, $0) }, digits: 0,
                                unit: "% of your calibrated maximum squeeze",
                                stale: fresh != .fresh,
                                note: activationNote(v)) {
                        ProgressView(value: min(100, max(0, v?.activationBarPct ?? v?.activationPct ?? 0)), total: 100)
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
                    ToolCard(fresh: fresh == .fresh)
                    EventsCard()
                }

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 12)], spacing: 12) {
                    ChartCard(title: "Grip effort, last 30 s", series: backend.activation, color: Theme.activation,
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

    private func activationNote(_ v: Telemetry?) -> String {
        let calibrated = backend.calibration != nil || backend.latest?.calibrated == true
        guard calibrated else {
            return backend.needsRecalibration ? "The wearable restarted, so the calibration was dropped. Calibrate again."
                                              : "Not calibrated. Run calibration to see grip effort."
        }
        var parts = [backend.calibration != nil ? "Calibrated in this app." : "Calibrated before this screen opened."]
        if v?.aboveMax == true { parts.append("Above your calibrated maximum.") }
        if v?.belowRest == true { parts.append("Below your relaxed baseline.") }
        if v?.degraded == true { parts.append("Some samples clipped: treat with caution.") }
        parts.append("Muscle activity, not grip force.")
        return parts.joined(separator: " ")
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

/// Tool use, estimated from vibration (provisional thresholds), with the New shift button.
struct ToolCard: View {
    @Environment(Backend.self) private var backend
    let fresh: Bool
    @State private var confirmShift = false

    var body: some View {
        let t = backend.tool
        let live = t != nil && t?.saved == false && backend.connected
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("Tool use").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
                Text("estimated from vibration").font(.caption).foregroundStyle(Theme.muted.opacity(0.8))
            }
            Text(live && fresh ? (t?.stateText ?? "—") : "—")
                .font(.system(size: 34, weight: .semibold))
                .tracking(-0.5)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 8) {
                fact("Tool time this session", seconds(t?.sessionToolS) + (t?.saved == true ? " (last known)" : ""))
                fact("Bouts", t?.bouts.map(String.init) ?? "—")
                fact("Longest bout", seconds(t?.longestBoutS))
                fact("Dominant frequency", live ? (t?.dominantHz.map { "\(Int($0.rounded())) Hz" } ?? "—") : "—")
            }
            ForEach(live ? (t?.warnings ?? []) : [], id: \.self) { w in
                Label(Translate.warningText[w] ?? w.replacingOccurrences(of: "_", with: " "), systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Theme.warning.opacity(0.14)))
            }
            if t?.saved == true {
                Text("Last known before the backend restarted.").font(.footnote).foregroundStyle(Theme.muted)
            }
            Text("Thresholds are provisional team choices, tuned on a phone motor, not regulatory limits.")
                .font(.footnote).foregroundStyle(Theme.muted)
            Button("New shift") { confirmShift = true }
                .buttonStyle(OutlineButtonStyle())
                .disabled(!backend.connected)
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .panel()
        .confirmationDialog("Start a new shift?", isPresented: $confirmShift, titleVisibility: .visible) {
            Button("Start new shift", role: .destructive) { Task { await backend.newShift() } }
        } message: {
            Text("Tool time and bouts go back to zero.")
        }
    }

    private func fact(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(Theme.muted)
            Text(value).font(.subheadline.weight(.semibold)).monospacedDigit()
        }
    }

    private func seconds(_ s: Double?) -> String {
        guard let s else { return "—" }
        return s < 60 ? "\(Int(s.rounded())) s" : "\(Int(s) / 60) min \(String(format: "%02d", Int(s.rounded()) % 60)) s"
    }
}

/// The latest events from the processor, in plain words (restarts, gaps, stale data, calibration steps).
struct EventsCard: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Events").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
            if backend.events.isEmpty {
                Text("Nothing yet.").font(.subheadline).foregroundStyle(Theme.muted)
            } else {
                ForEach(backend.events.prefix(8)) { e in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(e.at.formatted(date: .omitted, time: .standard)).font(.caption.monospaced()).foregroundStyle(Theme.muted)
                        Text(e.text).font(.subheadline).foregroundStyle(e.isWarning ? Theme.destructive : Theme.foreground)
                    }
                }
            }
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .panel()
    }
}
