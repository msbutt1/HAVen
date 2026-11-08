import Foundation

/// The Summary tab's numbers, computed from the saved history (the same items as the History tab), with the same
/// rules as the web dashboard's Overview (dashboard/js/overview.js). Pure, so it can be unit-tested.
/// No safe/unsafe line and no good/bad colour: there are no validated limits behind these numbers.
enum Summary {
    enum Period: Int, CaseIterable, Identifiable {
        case week = 7, month = 30, all = 0
        var id: Int { rawValue }
        var label: String { self == .week ? "Last 7 days" : self == .month ? "Last 30 days" : "All time" }
    }

    enum SourceFilter: String, CaseIterable, Identifiable {
        case all, live, replay, simulated
        var id: String { rawValue }
        var label: String { self == .all ? "All sources" : "\(rawValue.capitalized) only" }
        func keeps(_ p: Provenance) -> Bool { self == .all || p.rawValue == rawValue }
    }

    struct Slice: Equatable {
        var sessions: [SessionRecord]      // oldest first
        var tests: [StationResult]
    }

    struct Stats: Equatable {
        var recordings = 0
        var recordedS = 0.0
        var toolS: Double?                 // nil when no recording measured tool use
        var toolShare: Double?             // of the recorded time of the recordings that measured it
        var peakVibration: Double?
        var meanGrip: Double?              // missing grip is left out, never counted as 0
        var tests = 0
        var lastThreshold: Double?
    }

    struct Window: Equatable {
        var from: Date
        var to: Date
        var previousFrom: Date?            // nil for "All time": nothing to compare with
    }

    /// Whole calendar days ending today: "Last 7 days" is today and the 6 days before; the previous period is the
    /// 7 days before that.
    static func window(_ period: Period, now: Date, items: [HistoryItem], calendar: Calendar = .current) -> Window {
        let today = calendar.startOfDay(for: now)
        let to = calendar.date(byAdding: .day, value: 1, to: today)!
        if period == .all {
            let first = items.map(\.date).filter { $0 != .distantPast }.min() ?? now
            return Window(from: calendar.startOfDay(for: min(first, now)), to: to, previousFrom: nil)
        }
        let from = calendar.date(byAdding: .day, value: -period.rawValue, to: to)!
        return Window(from: from, to: to, previousFrom: calendar.date(byAdding: .day, value: -period.rawValue, to: from)!)
    }

    static func slice(_ items: [HistoryItem], from: Date, to: Date, source: SourceFilter) -> Slice {
        var s = Slice(sessions: [], tests: [])
        for item in items where item.date != .distantPast && item.date >= from && item.date < to {
            switch item {
            case .session(let r) where source.keeps(r.provenance): s.sessions.append(r)
            case .test(let t) where source.keeps(t.provenance): s.tests.append(t)
            default: break
            }
        }
        s.sessions.sort { ($0.endedAt ?? .distantPast) < ($1.endedAt ?? .distantPast) }
        s.tests.sort { ($0.completedAt ?? .distantPast) < ($1.completedAt ?? .distantPast) }
        return s
    }

    static func stats(_ s: Slice) -> Stats {
        var out = Stats()
        out.recordings = s.sessions.count
        out.recordedS = s.sessions.reduce(0) { $0 + $1.durationS }
        let withTool = s.sessions.filter { $0.toolTimeS != nil }
        if !withTool.isEmpty {
            let tool = withTool.reduce(0) { $0 + ($1.toolTimeS ?? 0) }
            let base = withTool.reduce(0) { $0 + $1.durationS }
            out.toolS = tool
            out.toolShare = base > 0 ? tool / base : nil
        }
        out.peakVibration = s.sessions.compactMap(\.peakVibration).max()
        let grips = s.sessions.compactMap(\.meanActivationPct)
        out.meanGrip = grips.isEmpty ? nil : grips.reduce(0, +) / Double(grips.count)
        out.tests = s.tests.count
        out.lastThreshold = s.tests.last(where: { $0.summary.threshold != nil })?.summary.threshold
        return out
    }

    /// "▲ 12 %", "▼ 3 %", "± 0 %", "new" (nothing before), or "—" (no number to compare).
    static func change(_ current: Double?, _ previous: Double?) -> String {
        guard let c = current, let p = previous else { return "—" }
        if p == 0 { return c == 0 ? "± 0 %" : "new" }
        let pct = (c - p) / abs(p) * 100
        if abs(pct) < 0.5 { return "± 0 %" }
        return "\(pct > 0 ? "▲" : "▼") \(Int(abs(pct).rounded())) %"
    }

    /// Recordings per day and source, for the stacked bars (days with none are left out).
    static func perDay(_ s: Slice, calendar: Calendar = .current) -> [DayCount] {
        var counts: [DayCount.Key: Int] = [:]
        for r in s.sessions {
            guard let d = r.endedAt ?? r.startedAt else { continue }
            counts[DayCount.Key(day: calendar.startOfDay(for: d), source: r.provenance), default: 0] += 1
        }
        return counts.map { DayCount(day: $0.key.day, source: $0.key.source, count: $0.value) }
            .sorted { ($0.day, $0.source.rawValue) < ($1.day, $1.source.rawValue) }
    }

    struct DayCount: Identifiable, Equatable {
        struct Key: Hashable { let day: Date; let source: Provenance }
        let day: Date
        let source: Provenance
        let count: Int
        var id: String { "\(day.timeIntervalSince1970)-\(source.rawValue)" }
    }

    /// Felt / didn't feel / no answer counts of one test's trials.
    static func answers(_ t: StationResult) -> (felt: Int, notFelt: Int, missing: Int) {
        (t.trials.filter { $0.response == "felt" }.count,
         t.trials.filter { $0.response == "not_felt" }.count,
         t.trials.filter { $0.response == "missing" }.count)
    }

    /// 45 s, 12 min, 1 h 05 min.
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite else { return "—" }
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s) s" }
        if s < 3600 { return "\(Int((Double(s) / 60).rounded())) min" }
        return "\(s / 3600) h \(String(format: "%02d", Int((Double(s % 3600) / 60).rounded()))) min"
    }

    /// "Today, 3:17 PM", "Yesterday, 11:39 AM", "Thu 5 Nov, 10:37 AM".
    static func when(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        guard date != .distantPast else { return "—" }
        let time = date.formatted(date: .omitted, time: .shortened)
        if calendar.isDate(date, inSameDayAs: now) { return "Today, \(time)" }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) {
            return "Yesterday, \(time)"
        }
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        let day = sameYear ? date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
                           : date.formatted(.dateTime.day().month(.abbreviated).year())
        return "\(day), \(time)"
    }
}
