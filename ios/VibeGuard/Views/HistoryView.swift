import SwiftUI

struct HistoryView: View {
    @Environment(Backend.self) private var backend
    @State private var shown = 20

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 6) {
                    Eyebrow(text: "History")
                    Text("Saved recordings and tests").font(.title2.weight(.semibold))
                }
                Spacer()
                Button {
                    Task { await backend.loadHistory() }
                } label: {
                    Image(systemName: "arrow.clockwise").font(.title3)
                }
                .accessibilityLabel("Reload history")
                .disabled(!backend.connected)
            }

            if backend.history.isEmpty {
                Text(backend.connected ? (backend.historyLoaded ? "Nothing saved yet. Record on the Live tab or run a touch test." : "Loading…")
                     : "Connect to the laptop to see saved recordings.")
                    .font(.body).foregroundStyle(Theme.muted)
                    .panel()
            } else {
                // newest first, a page at a time
                VStack(spacing: 10) {
                    ForEach(backend.history.prefix(shown)) { item in HistoryRow(item: item) }
                }
                if backend.history.count > shown {
                    Button("Show older (\(backend.history.count - shown) more)") { shown += 20 }
                        .buttonStyle(OutlineButtonStyle(big: true))
                }
            }
        }
        .navigationTitle("History")
        .refreshable { await backend.loadHistory() }
    }
}

/// One saved recording or test: what, when, where the data came from, and its facts as label + value.
struct HistoryRow: View {
    let item: HistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(isTest ? "Touch test" : "Wearable recording").font(.headline)
                    Text(Summary.when(item.date)).font(.subheadline).foregroundStyle(Theme.muted)
                }
                Spacer()
                SourceTag(source: source)
            }
            LazyVGrid(columns: [GridItem(.flexible(), alignment: .topLeading), GridItem(.flexible(), alignment: .topLeading)],
                      alignment: .leading, spacing: 10) {
                ForEach(facts, id: \.0) { label, value in
                    VStack(alignment: .leading, spacing: 1) {
                        Text(label).font(.caption).foregroundStyle(Theme.muted)
                        Text(value).font(.body.weight(.semibold)).monospacedDigit()
                    }
                }
            }
            if let warning {
                Label(warning, systemImage: "exclamationmark.triangle").font(.subheadline).foregroundStyle(Theme.destructive)
            }
        }
        .panel(padding: 14)
        .accessibilityElement(children: .combine)
    }

    private var isTest: Bool { if case .test = item { return true } else { return false } }

    private var source: Provenance {
        switch item {
        case .session(let s): return s.provenance
        case .test(let t): return t.provenance
        }
    }

    private var facts: [(String, String)] {
        switch item {
        case .session(let s):
            var f = [("Length", Summary.duration(s.durationS))]
            if let a = s.meanActivationPct { f.append(("Mean grip effort", "\(formatNumber(a, 0)) %")) }
            if let v = s.peakVibration { f.append(("Peak vibration", "\(formatNumber(v, 2)) m/s²")) }
            if let tt = s.toolTimeS { f.append(("Tool time (estimated)", Summary.duration(tt))) }
            f.append(("Score (experimental)", s.scoreTotal != nil ? "\(formatScore(s.scoreTotal)) au"
                      : s.calibrated ? "none for this recording" : "none (not calibrated)"))
            return f
        case .test(let t):
            var f = [("Threshold", t.summary.threshold.map { "setting \(formatNumber($0, 0))" } ?? "not reached"),
                     ("Trials", "\(t.trials.count)")]
            if t.summary.missing > 0 { f.append(("No answer", "\(t.summary.missing)")) }
            f.append(("Type", t.mode == .preview ? "Quick test" : "Full test"))
            return f
        }
    }

    private var warning: String? {
        if case .session(let s) = item, s.endedReason != "stopped" { return "Ended early: \(s.endedReason)" }
        return nil
    }
}
