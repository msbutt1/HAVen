import SwiftUI

struct CalibrateView: View {
    @Environment(AppModel.self) private var backend

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { context in
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Eyebrow(text: "Calibration")
                    Text("Relax, then squeeze").font(.title2.weight(.semibold))
                    Text("Activation is shown against this person’s own relaxed reading and hard squeeze, for this electrode placement.")
                        .font(.subheadline).foregroundStyle(Theme.muted)
                }

                StepsRow(phase: backend.cal.phase)

                VStack(alignment: .leading, spacing: 12) {
                    Text(prompt).font(.title2.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                    let running = backend.cal.phase == .relax || backend.cal.phase == .squeeze
                    Text(running ? "\(Int(backend.cal.remaining.rounded(.up)))" : " ")
                        .font(.system(size: 56, weight: .semibold)).monospacedDigit()
                        .contentTransition(.numericText())
                    ProgressView(value: progress, total: 1).tint(Theme.primary)
                }
                .panel()

                HStack(spacing: 12) {
                    Button(backend.calibration == nil ? "Start calibration" : "Recalibrate") {
                        Task { await backend.calibrate() }
                    }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(backend.sleeve.kind != .ready)
                    if let hint {
                        Text(hint).font(.footnote).foregroundStyle(Theme.muted)
                    }
                }

                ChartCard(title: "Raw muscle signal (envelope), last 15 s", series: backend.rawSignal, color: Theme.activation,
                          now: context.date, window: 15, floorMax: 50)

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    ResultTile(title: "Rest level", value: formatNumber(backend.calibration?.rest, 1))
                    ResultTile(title: "Max squeeze level", value: formatNumber(backend.calibration?.max, 1))
                    ResultTile(title: "Calibrated", value: backend.calibration?.at.map { $0.formatted(date: .omitted, time: .shortened) } ?? "Not yet")
                }

                ToolCalibrationSection()
            }
        }
        .navigationTitle("Calibrate")
    }

    private var prompt: String {
        switch backend.cal.phase {
        case .relax: return "Relax your forearm completely."
        case .squeeze: return "Squeeze as hard as you can and hold it."
        case .done: return "Calibrated. Activation now shows as a percentage of that squeeze."
        case .failed: return backend.cal.message ?? "Calibration failed. Try again."
        case .idle: return "Put the sleeve on, rest your arm on the table, then start."
        }
    }

    private var progress: Double {
        switch backend.cal.phase {
        case .relax, .squeeze: return 1 - backend.cal.remaining / max(backend.cal.total, 0.1)
        case .done: return 1
        default: return 0
        }
    }

    private var hint: String? {
        switch backend.sleeve.kind {
        case .disconnected: return "Connect the sleeve first."
        case .recording: return "Stop recording before calibrating."
        case .error: return "Clear the sleeve error first."
        case .calibrating: return backend.cal.phase == .idle ? "A tool calibration step is running." : nil
        default: return nil
        }
    }
}

struct StepsRow: View {
    let phase: CalPhase

    var body: some View {
        HStack(spacing: 8) {
            step(1, "Relax", "3 s", active: phase == .relax, done: phase == .squeeze || phase == .done)
            step(2, "Squeeze", "3 s", active: phase == .squeeze, done: phase == .done)
            step(3, "Saved", nil, active: false, done: phase == .done)
        }
    }

    private func step(_ n: Int, _ title: String, _ sub: String?, active: Bool, done: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack {
                Circle().fill(done ? Theme.success : active ? Theme.primary : Theme.foreground.opacity(0.1))
                Text(done ? "✓" : "\(n)").font(.caption.weight(.semibold))
                    .foregroundStyle(done ? Theme.successForeground : active ? Theme.primaryForeground : Theme.foreground)
            }
            .frame(width: 24, height: 24)
            Text(title).font(.subheadline.weight(.medium))
            Text(sub ?? " ").font(.caption.monospaced()).foregroundStyle(Theme.muted)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(active ? Theme.primary.opacity(0.1) : .clear))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(active ? Theme.primary.opacity(0.5) : Theme.border))
        .foregroundStyle(active || done ? Theme.foreground : Theme.muted)
    }
}

struct ResultTile: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(Theme.muted)
            Text(value).font(.title3.weight(.semibold)).monospacedDigit()
        }
        .panel(padding: 12)
    }
}

/// Tool calibration: teaches the detector this wearer's normal movement and this tool, so normal movement
/// (handshakes, waving) is never counted as tool use.
struct ToolCalibrationSection: View {
    @Environment(AppModel.self) private var backend

    struct Step: Identifiable { let id: String; let n: Int; let title: String; let secs: String; let how: String }
    private let steps: [Step] = [
        Step(id: "still", n: 1, title: "Still", secs: "5 s", how: "Arm resting, tool off."),
        Step(id: "movement", n: 2, title: "Normal movement", secs: "12 s", how: "Tool OFF. Shake hands, wave, swing the arm, tap the table, the whole time."),
        Step(id: "tool", n: 3, title: "Tool on", secs: "10 s", how: "Hold the running tool by its handle, as you would at work, the whole time."),
    ]

    var body: some View {
        let tc = backend.toolCal
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Eyebrow(text: "Tool calibration")
                Text("Tool vibration vs normal movement").font(.title2.weight(.semibold))
                Text("Three short steps. The app sets the tool thresholds between your normal movement and the tool, then checks that none of your movement would have counted as tool use.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }
            ForEach(steps) { step in
                let active = tc.phase == step.id
                let done = tc.done.contains(step.id)
                HStack(alignment: .top, spacing: 12) {
                    ZStack {
                        Circle().fill(done ? Theme.success : active ? Theme.primary : Theme.foreground.opacity(0.1))
                        Text(done ? "✓" : "\(step.n)").font(.caption.weight(.semibold))
                            .foregroundStyle(done ? Theme.successForeground : active ? Theme.primaryForeground : Theme.foreground)
                    }
                    .frame(width: 24, height: 24)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(step.title) (\(step.secs))").font(.subheadline.weight(.semibold))
                        Text(step.how).font(.footnote).foregroundStyle(Theme.muted)
                        if active {
                            Text("\(Int(tc.remaining.rounded(.up))) s left").font(.title3.weight(.semibold)).monospacedDigit()
                            ProgressView(value: 1 - tc.remaining / max(tc.total, 0.1), total: 1).tint(Theme.primary)
                        }
                    }
                    Spacer()
                    Button(active ? "Cancel" : done ? "Redo" : "Start") {
                        if active { backend.cancelToolCalibration() } else { Task { await backend.toolCalibrate(step.id) } }
                    }
                    .buttonStyle(OutlineButtonStyle())
                    .disabled(!backend.connected || (tc.phase != nil && !active) || (step.id == "tool" && !tc.done.contains("movement")))
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(active ? Theme.primary.opacity(0.1) : .clear))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(active ? Theme.primary.opacity(0.5) : Theme.border))
            }
            if let m = tc.message {
                Label(m, systemImage: "exclamationmark.triangle").font(.subheadline)
                    .foregroundStyle(Theme.destructive)
            }
            if let r = tc.result { ToolResultView(result: r, accepted: !tc.failed && backend.toolCalibrated) }
            HStack {
                Text(backend.toolCalibrated ? "Using thresholds calibrated on this phone." : "Using the default (provisional) thresholds.")
                    .font(.footnote).foregroundStyle(Theme.muted)
                Spacer()
                if backend.toolCalibrated { Button("Reset to defaults") { backend.resetToolCalibration() }.font(.footnote) }
            }
        }
        .panel()
    }
}

struct ToolResultView: View {
    let result: ToolCalibrationResult
    let accepted: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(accepted ? "Calibrated" : "Not applied").font(.headline)
                .foregroundStyle(accepted ? Theme.success : Theme.destructive)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    Text("median"); Text("tool band"); Text("arm"); Text("tool/arm"); Text("periodicity")
                }.font(.caption).foregroundStyle(Theme.muted)
                ForEach(["still", "movement", "tool"], id: \.self) { k in
                    if let q = result.summaries[k] {
                        GridRow {
                            Text(k)
                            Text(formatNumber(q.toolBand.median, 2))
                            Text(formatNumber(q.motionBand.median, 2))
                            Text(formatNumber(q.ratio.median, 2))
                            Text(formatNumber(q.concentration.median, 2))
                        }.font(.caption.monospacedDigit())
                    }
                }
            }
            Text("Tool windows accepted \(formatNumber(result.toolWindowsAccepted * 100, 0)) % · movement windows accepted \(formatNumber(result.movementWindowsAccepted * 100, 0)) % · false tool bouts during movement \(result.movementFalseBouts)")
                .font(.caption).foregroundStyle(Theme.muted)
            if !result.thresholds.isEmpty {
                Text("\(accepted ? "Thresholds" : "Proposed thresholds"): tool band ≥ \(formatNumber(result.thresholds["on_threshold_ms2"], 2)) m/s², tool/arm ≥ \(formatNumber(result.thresholds["tool_to_motion_min"], 2)), periodicity ≥ \(formatNumber(result.thresholds["concentration_min"], 2)), steady for 2 s")
                    .font(.caption).foregroundStyle(Theme.muted)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.section))
    }
}
