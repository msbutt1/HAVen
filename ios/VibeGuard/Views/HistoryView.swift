import SwiftUI

struct HistoryView: View {
    @Environment(Backend.self) private var backend

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
                    Image(systemName: "arrow.clockwise")
                }
                .accessibilityLabel("Reload history")
                .disabled(!backend.connected)
            }

            if backend.history.isEmpty {
                Text(backend.connected ? (backend.historyLoaded ? "Nothing saved yet. Record a session on the Live screen or run a perception test." : "Loading…")
                     : "Connect to the backend to see saved recordings.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
                    .panel()
            } else {
                VStack(spacing: 10) {
                    ForEach(backend.history) { item in HistoryRow(item: item) }
                }
            }
        }
        .navigationTitle("History")
        .refreshable { await backend.loadHistory() }
    }
}

struct HistoryRow: View {
    let item: HistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: isTest ? "hand.point.up.left" : "waveform.path.ecg")
                    .foregroundStyle(Theme.primary)
                Text(isTest ? "Perception test" : "Wearable recording").font(.headline)
                Spacer()
                SourceTag(source: source)
            }
            Text(item.date == .distantPast ? "—" : item.date.formatted(date: .abbreviated, time: .shortened))
                .font(.caption.monospaced()).foregroundStyle(Theme.muted)
            Text(summary).font(.subheadline).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
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

    private var summary: String {
        switch item {
        case .session(let s):
            var bits = [String(format: "%d:%02d long", Int(s.durationS) / 60, Int(s.durationS) % 60)]
            bits.append(s.scoreTotal != nil ? "score \(formatScore(s.scoreTotal)) au (experimental)"
                        : s.calibrated ? "no score (the backend could not attribute one to this recording)" : "no score (not calibrated)")
            if let a = s.meanActivationPct { bits.append("mean activation \(formatNumber(a, 0)) %") }
            if let v = s.peakVibration { bits.append("peak vibration \(formatNumber(v, 2)) m/s²") }
            if let tt = s.toolTimeS { bits.append("tool time \(Int(tt.rounded())) s (estimated)") }
            if s.endedReason != "stopped" { bits.append("ended early: \(s.endedReason)") }
            return bits.joined(separator: ", ")
        case .test(let t):
            var bits = [t.summary.threshold.map { "threshold setting \(formatNumber($0, 0))" } ?? "no threshold reached"]
            bits.append("\(t.trials.count) trials")
            if t.summary.missing > 0 { bits.append("\(t.summary.missing) missing") }
            bits.append(t.mode == .preview ? "short preview" : "full session")
            return bits.joined(separator: ", ")
        }
    }
}
