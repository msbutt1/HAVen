import XCTest
@testable import VibeGuard

/// The Summary tab's numbers (same rules as the web dashboard's Overview) and finding the laptop.
final class SummaryTests: XCTestCase {
    private var cal: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }
    private let now = ISO8601DateFormatter().date(from: "2026-11-08T18:00:00Z")!

    private func day(_ offset: Int, hour: Int = 10) -> Date {
        cal.date(byAdding: .hour, value: hour, to: cal.date(byAdding: .day, value: offset, to: cal.startOfDay(for: now))!)!
    }

    private func session(_ id: String, _ end: Date, _ source: Provenance = .live, duration: Double = 600, grip: Double? = 50,
                         peak: Double? = 90, vib: Double? = 4, tool: Double? = 300, score: Double? = 2) -> HistoryItem {
        .session(SessionRecord(id: id, deviceID: "d", provenance: source, startedAt: end.addingTimeInterval(-duration), endedAt: end,
                               durationS: duration, endedReason: "stopped", calibrated: grip != nil, scoreTotal: score,
                               meanActivationPct: grip, peakActivationPct: peak, peakVibration: vib, toolTimeS: tool))
    }

    private func test(_ id: String, _ end: Date, threshold: Double?, responses: [String]) -> HistoryItem {
        let trials = responses.enumerated().map { TrialResult(id: "\(id)-\($0.offset)", index: $0.offset + 1, setting: 100,
                                                               isCatch: false, response: $0.element, rtMs: nil) }
        let summary = StaircaseSummary(reversals: [], reversalCount: 0, threshold: threshold, catchTrials: 0, catchFelt: 0,
                                       missing: responses.filter { $0 == "missing" }.count)
        return .test(StationResult(id: id, provenance: .simulated, mode: .preview, startedAt: end, completedAt: end,
                                   trials: trials, summary: summary))
    }

    func testWindowIsWholeDaysAndThePreviousPeriodIsJustBefore() {
        let w = Summary.window(.week, now: now, items: [], calendar: cal)
        XCTAssertEqual(w.to, day(1, hour: 0))
        XCTAssertEqual(w.from, day(-6, hour: 0))              // today and the 6 days before
        XCTAssertEqual(w.previousFrom, day(-13, hour: 0))
        XCTAssertNil(Summary.window(.all, now: now, items: [], calendar: cal).previousFrom)
    }

    func testStatsLeaveMissingValuesOutAndShareToolTimeOnlyWhereMeasured() {
        let items = [session("a", day(0), tool: 300), session("b", day(-1), .replay, duration: 1200, grip: nil, peak: nil, vib: 6, tool: nil),
                     test("t1", day(-2), threshold: 80, responses: ["felt", "felt", "not_felt", "missing"]),
                     test("t0", day(-3), threshold: nil, responses: [])]
        let w = Summary.window(.week, now: now, items: items, calendar: cal)
        let s = Summary.slice(items, from: w.from, to: w.to, source: .all)
        XCTAssertEqual(s.sessions.map(\.id), ["b", "a"])                  // oldest first
        let st = Summary.stats(s)
        XCTAssertEqual(st.recordings, 2)
        XCTAssertEqual(st.recordedS, 1800)
        XCTAssertEqual(st.toolS, 300)
        XCTAssertEqual(st.toolShare, 0.5)                                 // of the 600 s that measured it, not of 1800 s
        XCTAssertEqual(st.peakVibration, 6)
        XCTAssertEqual(st.meanGrip, 50)                                   // the recording without grip is left out
        XCTAssertEqual(st.tests, 2)
        XCTAssertEqual(st.lastThreshold, 80)
        let a = Summary.answers(s.tests.last!)
        XCTAssertEqual([a.felt, a.notFelt, a.missing], [2, 1, 1])

        let live = Summary.stats(Summary.slice(items, from: w.from, to: w.to, source: .live))
        XCTAssertEqual(live.recordings, 1)
        XCTAssertEqual(live.tests, 0)
    }

    func testChangeIsNeutralWordsAndArrows() {
        XCTAssertEqual(Summary.change(12, 10), "▲ 20 %")
        XCTAssertEqual(Summary.change(9, 10), "▼ 10 %")
        XCTAssertEqual(Summary.change(10, 10), "± 0 %")
        XCTAssertEqual(Summary.change(3, 0), "new")
        XCTAssertEqual(Summary.change(0, 0), "± 0 %")
        XCTAssertEqual(Summary.change(nil, 4), "—")
    }

    func testPerDayCountsBySource() {
        let items = [session("a", day(0, hour: 9)), session("b", day(0, hour: 15)), session("c", day(0, hour: 16), .simulated),
                     session("d", day(-2))]
        let s = Summary.slice(items, from: day(-6, hour: 0), to: day(1, hour: 0), source: .all)
        let d = Summary.perDay(s, calendar: cal)
        XCTAssertEqual(d.map { "\($0.source.rawValue):\($0.count)" }, ["live:1", "live:2", "simulated:1"])
    }

    func testDurationsAndDates() {
        XCTAssertEqual(Summary.duration(45), "45 s")
        XCTAssertEqual(Summary.duration(2880), "48 min")
        XCTAssertEqual(Summary.duration(3840), "1 h 04 min")
        XCTAssertEqual(Summary.duration(nil), "—")
        XCTAssertTrue(Summary.when(day(0), now: now, calendar: cal).hasPrefix("Today, "))
        XCTAssertTrue(Summary.when(day(-1), now: now, calendar: cal).hasPrefix("Yesterday, "))
        XCTAssertEqual(Summary.when(.distantPast, now: now, calendar: cal), "—")
    }

    func testTypedAddressesBecomeBaseURLs() {
        XCTAssertEqual(TypedAddress.baseURL("192.168.1.23"), "http://192.168.1.23:8000")
        XCTAssertEqual(TypedAddress.baseURL(" 192.168.1.23:8010 "), "http://192.168.1.23:8010")
        XCTAssertEqual(TypedAddress.baseURL("http://Laptop.local:8000/dashboard/"), "http://laptop.local:8000")
        XCTAssertEqual(TypedAddress.baseURL("https://vg.example.org"), "https://vg.example.org")
        XCTAssertNil(TypedAddress.baseURL(""))
        XCTAssertNil(TypedAddress.baseURL("192.168.1.23 8000"))
        XCTAssertNil(TypedAddress.baseURL("ftp://192.168.1.23"))
    }

    func testBonjourAnnouncement() {
        XCTAssertTrue(BonjourTXT.password(["v": "1", "password": "1", "path": "/dashboard/"]))
        XCTAssertFalse(BonjourTXT.password(["password": "0"]))
        XCTAssertFalse(BonjourTXT.password([:]))
        XCTAssertEqual(BonjourTXT.baseURL(host: "192.168.1.5", port: 8000), "http://192.168.1.5:8000")
        XCTAssertEqual(BonjourTXT.baseURL(host: "fe80::1%en0", port: 8000), "http://[fe80::1]:8000")
        XCTAssertEqual(BonjourTXT.serviceType, "_vibeguard._tcp")
    }
}
