import Charts
import SwiftUI

func formatScore(_ v: Double?) -> String {
    guard let v else { return "—" }
    return abs(v) < 1 ? String(format: "%.3f", v) : String(format: "%.1f", v)
}

func formatDuration(_ s: Double?) -> String {
    guard let s, s.isFinite else { return "—" }
    let t = Int(s.rounded(.down))
    return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60) : String(format: "%d:%02d", t / 60, t % 60)
}

/// Tool use vs normal arm use, from the vibration the sleeve measures.
struct ToolCard: View {
    let tool: ToolState?
    let calibrated: Bool

    var body: some View {
        let inUse = tool?.state == "tool"
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("Tool use").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
                Text(calibrated ? "calibrated for this tool" : "default thresholds")
                    .font(.caption).foregroundStyle(Theme.muted)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .overlay(Capsule().strokeBorder(Theme.border))
                Spacer()
            }
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Circle().fill(inUse ? Theme.warning : tool?.state == "no_tool" ? Theme.success : Theme.muted.opacity(0.5))
                    .frame(width: 12, height: 12)
                Text(tool == nil || tool?.state == "unknown" ? "—" : inUse ? "Tool in use" : "No tool")
                    .font(.system(size: 34, weight: .semibold))
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), spacing: 10)], alignment: .leading, spacing: 10) {
                stat("Tool time this shift", formatDuration(tool?.sessionToolS))
                stat("Current bout", inUse ? formatDuration(tool?.boutS) : "—")
                stat("Bouts", tool.map { "\($0.bouts)" } ?? "—")
                stat("Longest bout", formatDuration(tool?.longestBoutS))
            }
            ForEach(tool?.warnings ?? [], id: \.self) { w in
                Label(AppModel.warningText[w] ?? w, systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline.weight(.medium))
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.warning.opacity(0.18)))
            }
            Text(detail).font(.caption.monospaced()).foregroundStyle(Theme.muted)
            Text("Estimated from vibration: steady, repeating vibration counts as a tool; handshakes, waving and knocks do not. Warnings are team settings, not regulatory limits.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
        .panel()
        .accessibilityElement(children: .combine)
    }

    private var detail: String {
        guard let t = tool, t.state != "unknown" else { return "Waiting for motion data." }
        return "tool band \(formatNumber(t.toolBand, 2)) m/s² @ \(formatNumber(t.dominantHz, 0)) Hz · arm \(formatNumber(t.motionBand, 2)) m/s² · periodicity \(formatNumber(t.concentration, 2))"
    }

    private func stat(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(Theme.muted)
            Text(value).font(.title3.weight(.semibold)).monospacedDigit()
        }
    }
}

/// Only with the simulated sleeve: make it squeeze, move the arm, or run a tool.
struct DemoControls: View {
    @Environment(AppModel.self) private var backend
    @State private var tool = false
    @State private var arm = false
    @State private var squeezing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Simulated sleeve controls").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
            HStack(spacing: 10) {
                Text(squeezing ? "Squeezing" : "Hold to squeeze")
                    .font(.subheadline.weight(.medium))
                    .padding(.horizontal, 14).frame(height: 40)
                    .background(Capsule().fill(squeezing ? Theme.primary.opacity(0.25) : Theme.section))
                    .overlay(Capsule().strokeBorder(Theme.border))
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { _ in if !squeezing { squeezing = true; backend.demo.squeeze = true } }
                        .onEnded { _ in squeezing = false; backend.demo.squeeze = false })
                Toggle("Tool running", isOn: $tool).onChange(of: tool) { _, v in backend.demo.tool = v }
                Toggle("Arm moving", isOn: $arm).onChange(of: arm) { _, v in backend.demo.armMoving = v }
            }
            .toggleStyle(.button)
        }
        .panel(padding: 12)
    }
}

func formatNumber(_ v: Double?, _ digits: Int) -> String {
    guard let v, v.isFinite else { return "—" }
    return String(format: "%.\(digits)f", v)
}

struct FreshnessLine: View {
    @Environment(AppModel.self) private var backend
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
        case .none: return backend.connected ? "Connected. Waiting for the first reading." : "No data. Press Start, and power the sleeve."
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
