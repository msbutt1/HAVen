import Charts
import SwiftUI

/// KPI tiles and charts over the saved recordings and perception tests (the web dashboard's Overview, in Swift Charts).
/// Computed on the phone from the same items as the History tab. Changes are shown in neutral ink and no chart draws
/// a safe or unsafe line: there are no validated limits behind these numbers.
struct SummaryView: View {
    @Environment(Backend.self) private var backend
    @State private var period: Summary.Period = .month
    @State private var source: Summary.SourceFilter = .all

    private static let sources: [Provenance] = [.live, .replay, .simulated]
    private static let sourceColors: [Color] = [Theme.ovGreen, Theme.ovBlue, Theme.ovViolet]

    var body: some View {
        let now = Date()
        let w = Summary.window(period, now: now, items: backend.history)
        let s = Summary.slice(backend.history, from: w.from, to: w.to, source: source)
        let cur = Summary.stats(s)
        let prev = w.previousFrom.map { Summary.stats(Summary.slice(backend.history, from: $0, to: w.from, source: source)) }
        let simulated = s.sessions.filter { $0.provenance == .simulated }.count + s.tests.filter { $0.provenance == .simulated }.count
        let total = s.sessions.count + s.tests.count

        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Eyebrow(text: "Summary")
                Text("Recordings and tests").font(.title2.weight(.semibold))
                Text(total == 0 ? (backend.connected ? "Nothing saved in this period. Record on the Live tab or run a touch test." : "Connect to the laptop to see saved recordings.")
                     : "\(cur.recordings) recording\(cur.recordings == 1 ? "" : "s") and \(cur.tests) touch test\(cur.tests == 1 ? "" : "s"). Same items as the History tab.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }

            HStack(spacing: 10) {
                Picker("Period", selection: $period) {
                    ForEach(Summary.Period.allCases) { Text($0.label).tag($0) }
                }
                Picker("Data", selection: $source) {
                    ForEach(Summary.SourceFilter.allCases) { Text($0.label).tag($0) }
                }
            }
            .pickerStyle(.menu)
            .font(.body)

            if simulated > 0 {
                Text(simulated == total ? "All of this is simulated data, not measurements from a person."
                     : "\(simulated) of \(total) items here are simulated, not measurements from a person.")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.white)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Stripes(width: 12).clipShape(RoundedRectangle(cornerRadius: 10)))
            }

            kpis(cur, prev, compare: period == .all ? nil : "previous \(period.rawValue) days")

            ChartPanel(title: "Recordings per day") { perDay(s) }
            ChartPanel(title: "Grip effort per recording", subtitle: "mean and peak, % of calibration") { grip(s) }
            ChartPanel(title: "Highest exposure scores", experimental: true) { scores(s) }
            ChartPanel(title: "Recording time with a tool", subtitle: "estimated from vibration") { tool(s, cur) }
            ChartPanel(title: "Touch test answers", subtitle: "newest tests") { answers(s) }
            ChartPanel(title: "Grip effort vs peak vibration", subtitle: "each dot is a recording") { scatter(s) }
            ChartPanel(title: "Peak vibration per recording", subtitle: "m/s², unweighted") { vibration(s) }
            ChartPanel(title: "Touch test threshold", subtitle: "motor setting, higher means less sensitive") { thresholds(s) }
            ChartPanel(title: "Where the data came from") { sourcesChart(s) }

            Text("Measured vibration is unweighted (not ISO 5349). Grip effort is relative to each wearer’s calibration, not force. The exposure score is experimental and not a dose. No chart draws a safe or unsafe line: there are no validated limits behind these numbers.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
        .navigationTitle("Summary")
        .refreshable { await backend.loadHistory() }
    }

    // MARK: - KPI tiles

    private func kpis(_ c: Summary.Stats, _ p: Summary.Stats?, compare: String?) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            KPITile(title: "Recordings", value: "\(c.recordings)", unit: nil,
                    change: p.map { Summary.change(Double(c.recordings), Double($0.recordings)) },
                    previous: p.map { "\($0.recordings)" }, compare: compare, extra: nil)
            KPITile(title: "Recorded time", value: Summary.duration(c.recordedS), unit: nil,
                    change: p.map { Summary.change(c.recordedS, $0.recordedS) }, previous: p.map { Summary.duration($0.recordedS) },
                    compare: compare, extra: nil)
            KPITile(title: "Tool time (estimated)", value: Summary.duration(c.toolS), unit: nil,
                    change: p.map { Summary.change(c.toolS, $0.toolS) }, previous: p.map { Summary.duration($0.toolS) },
                    compare: compare, extra: c.toolShare.map { "\(Int(($0 * 100).rounded())) % of recorded time" })
            KPITile(title: "Highest peak vibration", value: formatNumber(c.peakVibration, 1), unit: "m/s²",
                    change: p.map { Summary.change(c.peakVibration, $0.peakVibration) },
                    previous: p.map { formatNumber($0.peakVibration, 1) }, compare: compare, extra: "unweighted")
            KPITile(title: "Mean grip effort", value: formatNumber(c.meanGrip, 0), unit: "%",
                    change: p.map { Summary.change(c.meanGrip, $0.meanGrip) }, previous: p.map { formatNumber($0.meanGrip, 0) },
                    compare: compare, extra: "of each wearer’s calibration")
            KPITile(title: "Touch tests", value: "\(c.tests)", unit: nil,
                    change: p.map { Summary.change(Double(c.tests), Double($0.tests)) }, previous: p.map { "\($0.tests)" },
                    compare: compare, extra: c.lastThreshold.map { "latest threshold: \(Int($0.rounded()))" })
        }
    }

    // MARK: - charts

    @ViewBuilder private func perDay(_ s: Summary.Slice) -> some View {
        let days = Summary.perDay(s)
        if days.isEmpty { EmptyChart(text: "No recordings in this period.") } else {
            Chart(days) { d in
                BarMark(x: .value("Day", d.day, unit: .day), y: .value("Recordings", d.count))
                    .foregroundStyle(by: .value("Source", d.source.label))
                    .cornerRadius(3)
            }
            .chartForegroundStyleScale(domain: Self.sources.map(\.label), range: Self.sourceColors)
            .chartLegend(Set(days.map(\.source)).count > 1 ? .visible : .hidden)
            .chartYAxis { AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) }
            .frame(height: 200)
        }
    }

    @ViewBuilder private func grip(_ s: Summary.Slice) -> some View {
        let rows = s.sessions.filter { $0.meanActivationPct != nil && $0.endedAt != nil }
        if rows.isEmpty { EmptyChart(text: "No calibrated recordings in this period (grip effort needs a calibration).") } else {
            Chart {
                ForEach(rows) { r in
                    LineMark(x: .value("Recording", r.endedAt!), y: .value("Grip %", r.meanActivationPct!), series: .value("Series", "Mean"))
                        .foregroundStyle(by: .value("Series", "Mean"))
                        .interpolationMethod(.monotone)
                    if let peak = r.peakActivationPct {
                        LineMark(x: .value("Recording", r.endedAt!), y: .value("Grip %", peak), series: .value("Series", "Peak"))
                            .foregroundStyle(by: .value("Series", "Peak"))
                            .interpolationMethod(.monotone)
                    }
                }
            }
            .chartForegroundStyleScale(domain: ["Mean", "Peak"], range: [Theme.ovBlue, Theme.ovBlue2])
            .chartYScale(domain: 0...max(100, (rows.compactMap(\.peakActivationPct).max() ?? 0)))
            .chartYAxis { AxisMarks(position: .leading, values: .automatic(desiredCount: 5)) { v in
                AxisGridLine(); AxisValueLabel { if let d = v.as(Double.self) { Text("\(Int(d)) %") } }
            } }
            .frame(height: 200)
        }
    }

    @ViewBuilder private func scores(_ s: Summary.Slice) -> some View {
        let rows = Array(s.sessions.filter { $0.scoreTotal != nil }.sorted { $0.scoreTotal! > $1.scoreTotal! }.prefix(8))
        if rows.isEmpty { EmptyChart(text: "No scored recordings in this period (the score needs a calibration).") } else {
            let dp = rows.contains { $0.scoreTotal! < 10 } ? 2 : 1
            Chart(rows) { r in
                BarMark(x: .value("Score (au)", r.scoreTotal!), y: .value("Recording", Summary.when(r.endedAt ?? .distantPast)))
                    .foregroundStyle(Theme.ovTeal)
                    .cornerRadius(3)
                    .annotation(position: .trailing) {
                        Text(formatNumber(r.scoreTotal, dp)).font(.caption2).foregroundStyle(Theme.muted)
                    }
            }
            .chartXAxisLabel("au (arbitrary units)")
            .frame(height: CGFloat(rows.count) * 30 + 40)
        }
    }

    @ViewBuilder private func tool(_ s: Summary.Slice, _ c: Summary.Stats) -> some View {
        let measured = s.sessions.filter { $0.toolTimeS != nil }
        let base = measured.reduce(0) { $0 + $1.durationS }
        if base <= 0 { EmptyChart(text: "No tool-use data in this period.") } else {
            let t = min(base, c.toolS ?? 0)
            Chart {
                SectorMark(angle: .value("Time", t), innerRadius: .ratio(0.7), angularInset: 1.5)
                    .foregroundStyle(by: .value("Part", "Tool running (estimated)"))
                SectorMark(angle: .value("Time", base - t), innerRadius: .ratio(0.7), angularInset: 1.5)
                    .foregroundStyle(by: .value("Part", "No tool detected"))
            }
            .chartForegroundStyleScale(domain: ["Tool running (estimated)", "No tool detected"], range: [Theme.ovOrange, Theme.ovTrack])
            .chartBackground { _ in
                VStack(spacing: 0) {
                    Text("\(Int(((c.toolShare ?? 0) * 100).rounded())) %").font(.title.weight(.semibold))
                    Text("of recorded time").font(.caption).foregroundStyle(Theme.muted)
                }
            }
            .frame(height: 220)
        }
    }

    private struct Answer: Identifiable { let id: String; let test: String; let kind: String; let count: Int }

    @ViewBuilder private func answers(_ s: Summary.Slice) -> some View {
        let tests = Array(s.tests.suffix(6))
        if tests.isEmpty { EmptyChart(text: "No touch tests in this period.") } else {
            let rows: [Answer] = tests.flatMap { t -> [Answer] in
                let a = Summary.answers(t)
                let label = Summary.when(t.completedAt ?? .distantPast)
                return [Answer(id: t.id + "f", test: label, kind: "Felt it", count: a.felt),
                        Answer(id: t.id + "n", test: label, kind: "Didn’t feel it", count: a.notFelt),
                        Answer(id: t.id + "m", test: label, kind: "No answer", count: a.missing)]
            }
            Chart(rows) { r in
                BarMark(x: .value("Trials", r.count), y: .value("Test", r.test))
                    .foregroundStyle(by: .value("Answer", r.kind))
            }
            .chartForegroundStyleScale(domain: ["Felt it", "Didn’t feel it", "No answer"], range: [Theme.ovBlue, Theme.ovOrange, Theme.ovNeutral])
            .frame(height: CGFloat(tests.count) * 34 + 60)
        }
    }

    @ViewBuilder private func scatter(_ s: Summary.Slice) -> some View {
        let rows = s.sessions.filter { $0.peakVibration != nil && $0.meanActivationPct != nil }
        if rows.isEmpty { EmptyChart(text: "No recordings with both grip effort and vibration in this period.") } else {
            Chart(rows) { r in
                PointMark(x: .value("Peak vibration (m/s²)", r.peakVibration!), y: .value("Mean grip (%)", r.meanActivationPct!))
                    .foregroundStyle(by: .value("Source", r.provenance.label))
                    .symbolSize(70)
            }
            .chartForegroundStyleScale(domain: Self.sources.map(\.label), range: Self.sourceColors)
            .chartLegend(Set(rows.map(\.provenance)).count > 1 ? .visible : .hidden)
            .chartXAxisLabel("Peak vibration (m/s²)")
            .chartYAxisLabel("Mean grip effort (%)")
            .frame(height: 220)
        }
    }

    @ViewBuilder private func vibration(_ s: Summary.Slice) -> some View {
        let rows = s.sessions.filter { $0.peakVibration != nil && $0.endedAt != nil }
        if rows.isEmpty { EmptyChart(text: "No vibration readings in this period.") } else {
            Chart(rows) { r in
                LineMark(x: .value("Recording", r.endedAt!), y: .value("m/s²", r.peakVibration!))
                    .foregroundStyle(Theme.ovOrange)
                    .interpolationMethod(.monotone)
            }
            .frame(height: 180)
        }
    }

    @ViewBuilder private func thresholds(_ s: Summary.Slice) -> some View {
        let rows = s.tests.filter { $0.summary.threshold != nil && $0.completedAt != nil }
        if rows.isEmpty { EmptyChart(text: "No touch test thresholds in this period.") } else {
            Chart(rows) { t in
                LineMark(x: .value("Test", t.completedAt!), y: .value("Setting", t.summary.threshold!))
                    .foregroundStyle(Theme.ovBlue)
                PointMark(x: .value("Test", t.completedAt!), y: .value("Setting", t.summary.threshold!))
                    .foregroundStyle(Theme.ovBlue)
            }
            .frame(height: 180)
        }
    }

    private struct SourceCount: Identifiable { let id: Provenance; let count: Int }

    @ViewBuilder private func sourcesChart(_ s: Summary.Slice) -> some View {
        let counts = Self.sources.map { p in
            SourceCount(id: p, count: s.sessions.filter { $0.provenance == p }.count + s.tests.filter { $0.provenance == p }.count)
        }.filter { $0.count > 0 }
        let total = counts.reduce(0) { $0 + $1.count }
        if total == 0 { EmptyChart(text: "Nothing saved in this period.") } else {
            Chart(counts) { c in
                SectorMark(angle: .value("Items", c.count), innerRadius: .ratio(0.7), angularInset: 1.5)
                    .foregroundStyle(by: .value("Source", c.id.label))
            }
            .chartForegroundStyleScale(domain: Self.sources.map(\.label), range: Self.sourceColors)
            .chartBackground { _ in
                VStack(spacing: 0) {
                    Text("\(total)").font(.title.weight(.semibold))
                    Text(total == 1 ? "saved item" : "saved items").font(.caption).foregroundStyle(Theme.muted)
                }
            }
            .frame(height: 220)
        }
    }
}

/// One KPI: the number, its change against the previous period (neutral ink), and the previous value.
struct KPITile: View {
    let title: String
    let value: String
    let unit: String?
    let change: String?
    let previous: String?
    let compare: String?
    let extra: String?

    var body: some View {
        VStack(spacing: 6) {
            Text(title).font(.footnote.weight(.medium)).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(value).font(.system(size: 30, weight: .semibold)).minimumScaleFactor(0.6).lineLimit(1)
                if let unit, value != "—" { Text(unit).font(.caption).foregroundStyle(Theme.muted) }
            }
            if let change, let previous, let compare {
                VStack(spacing: 1) {
                    Text(change).font(.caption.weight(.semibold))
                    Text("was \(previous), \(compare)").font(.caption2).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
                }
            }
            if let extra { Text(extra).font(.caption2).foregroundStyle(Theme.muted).multilineTextAlignment(.center) }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .panel(padding: 12)
        .accessibilityElement(children: .combine)
    }
}

struct ChartPanel<Content: View>: View {
    let title: String
    var subtitle: String?
    var experimental = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(title).font(.headline)
                if experimental { ExperimentalTag() }
            }
            if let subtitle { Text(subtitle).font(.caption).foregroundStyle(Theme.muted) }
            content()
        }
        .panel()
    }
}

struct EmptyChart: View {
    let text: String
    var body: some View {
        Text(text).font(.subheadline).foregroundStyle(Theme.muted)
            .frame(maxWidth: .infinity, minHeight: 80)
            .multilineTextAlignment(.center)
    }
}
