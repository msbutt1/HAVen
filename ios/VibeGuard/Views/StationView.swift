import SwiftUI

struct StationView: View {
    @Environment(Backend.self) private var backend
    @State private var mode: StationMode = .preview

    var body: some View {
        let ui = backend.stationUI
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Eyebrow(text: "Touch test")
                Text("Fingertip vibration test").font(.title2.weight(.semibold))
                Text(Backend.stationDetail).font(.footnote).foregroundStyle(Theme.muted)
            }

            switch ui.phase {
            case .idle: idle
            case .running: running(ui)
            case .complete: complete(ui)
            }

            if let message = ui.message {
                Text(message)
                    .font(.subheadline.weight(.medium))
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.destructive.opacity(0.1)))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.destructive.opacity(0.4)))
            }

            if !ui.trials.isEmpty { TrialLog(trials: ui.trials, reveal: ui.phase == .complete) }

            Text("Motor settings are drive levels sent to the motor, not measured vibration amplitudes.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
        .navigationTitle("Touch test")
    }

    private var idle: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Checks the softest buzz the person can feel. Rest a fingertip on the motor, then answer each time: felt it, or didn’t.")
                .font(.body)
            Text("Test length").font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
            ForEach(StationMode.allCases) { m in
                ModeCard(title: m == .preview ? "Quick test" : "Full test",
                         detail: m == .preview ? "About 30 seconds (6 trials)" : "A few minutes (until 8 reversals)",
                         selected: mode == m) { mode = m }
            }
            Button("Start test") { Task { await backend.startStationTest(mode: mode) } }
                .buttonStyle(PrimaryButtonStyle(big: true))
                .disabled(backend.station.kind != .ready)
            if backend.station.kind == .disconnected {
                Text("Connect to the laptop first.").font(.callout).foregroundStyle(Theme.muted)
            }
        }
        .panel()
    }

    private func running(_ ui: StationUI) -> some View {
        let current = ui.current
        let n = current?.index ?? ui.trials.count
        return VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text(n == 0 ? "Starting" : ui.planned.map { "Trial \(n) of \($0)" } ?? "Trial \(n)")
                    .font(.subheadline.monospaced()).foregroundStyle(Theme.muted)
                Spacer()
                Button("Stop test") { Task { await backend.abortStationTest() } }
                    .buttonStyle(PrimaryButtonStyle(role: .destructive))
            }
            Text(current == nil ? "Keep your fingertip on the motor. Next trial coming."
                 : current!.answered ? "Answer recorded." : "Did you feel a vibration?")
                .font(.title.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            TimelineView(.animation(minimumInterval: 0.05, paused: current == nil || current!.answered)) { ctx in
                let left = current.map { max(0, 1 - ctx.date.timeIntervalSince($0.receivedAt) / $0.window) } ?? 0
                ProgressView(value: current?.answered == false ? left : 0, total: 1).tint(Theme.primary)
            }
            let waiting = current != nil && current?.answered == false
            HStack(spacing: 12) {
                AnswerButton(title: "Felt it", systemImage: "checkmark", tint: Theme.success) {
                    Task { await backend.respond("felt") }
                }
                AnswerButton(title: "Didn’t feel it", systemImage: "xmark", tint: Theme.foreground) {
                    Task { await backend.respond("not_felt") }
                }
            }
            .disabled(!waiting)
            Text("No answer in time is saved as “no answer”, not as “didn’t feel it”.")
                .font(.callout).foregroundStyle(Theme.muted)
        }
        .panel()
    }

    private func complete(_ ui: StationUI) -> some View {
        let s = ui.result?.summary
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(formatNumber(s?.threshold, 0)).font(.system(size: 52, weight: .semibold)).monospacedDigit()
                Text("threshold, motor setting out of 255").font(.subheadline).foregroundStyle(Theme.muted)
            }
            Text(notes(s)).font(.subheadline).foregroundStyle(Theme.muted)
            Button("Run another test") { backend.resetStation() }
                .buttonStyle(OutlineButtonStyle())
        }
        .panel()
    }

    private func notes(_ s: StaircaseSummary?) -> String {
        guard let s else { return "" }
        var parts: [String] = []
        if s.threshold == nil { parts.append("Not enough reversals to estimate a threshold. Run the full session for one.") }
        else if s.reversals.count == 1 { parts.append("Based on a single reversal, so treat it as rough.") }
        else { parts.append("Average of the last \(s.reversals.count) reversal settings.") }
        if s.catchTrials > 0 { parts.append("Catch trials with no vibration: \(s.catchFelt) of \(s.catchTrials) answered “felt it”.") }
        if s.missing > 0 { parts.append("\(s.missing) missing response\(s.missing == 1 ? "" : "s").") }
        parts.append("Motor setting only; vibration amplitude was not measured. Simulated station: no motor was driven.")
        return parts.joined(separator: " ")
    }
}

struct TrialLog: View {
    let trials: [TrialResult]
    let reveal: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("TRIAL").frame(width: 50, alignment: .leading)
                Text("SETTING").frame(maxWidth: .infinity, alignment: .leading)
                Text("ANSWER").frame(maxWidth: .infinity, alignment: .leading)
                Text("TIME").frame(width: 70, alignment: .trailing)
            }
            .font(.system(size: 11, weight: .medium, design: .monospaced))
            .foregroundStyle(Theme.muted)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(Theme.section.opacity(0.6))
            ForEach(trials) { t in
                Divider().overlay(Theme.border)
                HStack {
                    Text("\(t.index)").frame(width: 50, alignment: .leading)
                    Text(t.isCatch ? (reveal ? "0 (catch)" : "—") : "\(t.setting)").frame(maxWidth: .infinity, alignment: .leading)
                    Text(answer(t.response)).frame(maxWidth: .infinity, alignment: .leading)
                    Text(t.rtMs.map { "\($0) ms" } ?? "—").frame(width: 70, alignment: .trailing)
                }
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(t.response == "missing" ? Theme.destructive : Theme.foreground)
                .padding(.horizontal, 12).padding(.vertical, 9)
            }
        }
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).strokeBorder(Theme.border))
    }

    private func answer(_ r: String) -> String {
        switch r {
        case "felt": return "Felt it"
        case "not_felt": return "Didn’t feel it"
        default: return "No response"
        }
    }
}

/// A test length to choose: a big card with a radio mark.
struct ModeCard: View {
    let title: String
    let detail: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .font(.title2)
                    .foregroundStyle(selected ? Theme.primary : Theme.muted)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline).foregroundStyle(Theme.foreground)
                    Text(detail).font(.subheadline).foregroundStyle(Theme.muted)
                }
                Spacer(minLength: 0)
            }
            .padding(14)
            .frame(minHeight: 64)
            .background(RoundedRectangle(cornerRadius: 12).fill(selected ? Theme.primary.opacity(0.1) : Theme.background))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(selected ? Theme.primary.opacity(0.6) : Theme.border))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// One of the two answers: very large, an icon over the word.
struct AnswerButton: View {
    let title: String
    let systemImage: String
    let tint: Color
    let action: () -> Void
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        Button(action: action) {
            VStack(spacing: 10) {
                Image(systemName: systemImage).font(.system(size: 36, weight: .bold))
                Text(title).font(.title3.weight(.semibold)).multilineTextAlignment(.center)
            }
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, minHeight: 140)
            .background(RoundedRectangle(cornerRadius: 14).fill(tint.opacity(enabled ? 0.12 : 0.05)))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(tint.opacity(enabled ? 0.55 : 0.2), lineWidth: 1.5))
            .opacity(enabled ? 1 : 0.5)
        }
        .buttonStyle(.plain)
    }
}
