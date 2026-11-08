import SwiftUI

/// The guide's two-step calibration (firmware/INTEGRATION_FOR_ABDULJABBAR.md, section 3): relaxed, then squeeze,
/// each with its own button and countdown; a failure says why in plain words and goes back to step 1.
struct CalibrateView: View {
    @Environment(Backend.self) private var backend

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { context in
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Eyebrow(text: "Calibration")
                    Text("Relax, then squeeze").font(.title2.weight(.semibold))
                    Text("Grip effort is shown against this person’s own relaxed level and maximum squeeze, for this electrode placement. It is muscle activity, not grip force.")
                        .font(.subheadline).foregroundStyle(Theme.muted)
                }

                if backend.needsRecalibration && backend.cal.phase == .idle {
                    Label("The wearable restarted, so the calibration was dropped. Calibrate again.", systemImage: "arrow.clockwise")
                        .font(.subheadline.weight(.medium))
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.warning.opacity(0.14)))
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

                buttons

                ChartCard(title: "Raw muscle signal (envelope), last 15 s", series: backend.rawSignal, color: Theme.activation,
                          now: context.date, window: 15, floorMax: 50)

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    ResultTile(title: "Relaxed level", value: formatNumber(backend.calibration?.rest, 0))
                    ResultTile(title: "Maximum squeeze level", value: formatNumber(backend.calibration?.max, 0))
                    ResultTile(title: "Calibrated", value: backend.calibration?.at.map { $0.formatted(date: .omitted, time: .shortened) } ?? "Not yet")
                }
            }
        }
        .navigationTitle("Calibrate")
    }

    @ViewBuilder private var buttons: some View {
        let phase = backend.cal.phase
        let running = phase == .relax || phase == .squeeze
        let ready = backend.sleeve.kind == .ready
        VStack(alignment: .leading, spacing: 12) {
            if phase == .relaxedCaptured {
                Button("Start squeeze") { Task { await backend.calibrateStep("squeeze") } }
                    .buttonStyle(PrimaryButtonStyle(big: true))
                    .disabled(!(ready || backend.sleeve.kind == .calibrating))
            } else if !running {
                Button(phase == .done || backend.calibration != nil ? "Recalibrate"
                       : phase == .failed ? "Start relaxed again" : "Start relaxed") {
                    Task { await backend.calibrateStep("relaxed") }
                }
                .buttonStyle(PrimaryButtonStyle(big: true))
                .disabled(!ready)
            }
            if running || phase == .relaxedCaptured {
                Button("Cancel") { Task { await backend.cancelCalibration() } }
                    .buttonStyle(OutlineButtonStyle(big: true))
            }
            if let hint {
                Text(hint).font(.callout).foregroundStyle(Theme.muted)
            }
        }
    }

    private var prompt: String {
        switch backend.cal.phase {
        case .relax: return "Relax your forearm completely, resting on a table. Hold still."
        case .relaxedCaptured: return "Relaxed level captured. Now squeeze as hard as you can and hold, then press Start squeeze."
        case .squeeze: return "Squeeze as hard as you can and hold it."
        case .done: return "Calibrated. Grip effort now shows as a percentage of that squeeze."
        case .failed: return backend.cal.message ?? "Calibration failed. Start again with the relaxed step."
        case .idle: return "Put the sleeve on, rest your arm on the table, then press Start relaxed."
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
        case .calibrating: return backend.cal.phase == .idle ? "The sleeve is calibrating (a replay reissues its calibration)." : nil
        default: return nil
        }
    }
}

struct StepsRow: View {
    let phase: CalPhase

    var body: some View {
        HStack(spacing: 8) {
            step(1, "Relaxed", "~3.5 s", active: phase == .relax, done: [.relaxedCaptured, .squeeze, .done].contains(phase))
            step(2, "Squeeze", "~3.5 s", active: phase == .squeeze || phase == .relaxedCaptured, done: phase == .done)
            step(3, "Calibrated", nil, active: false, done: phase == .done)
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
