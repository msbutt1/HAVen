import XCTest
@testable import HAVenEngine

func fixture(_ name: String) throws -> Any {
    let url = Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: nil)!
    return try JSONSerialization.jsonObject(with: Data(contentsOf: url))
}

final class FilterTests: XCTestCase {
    /// The Swift Butterworth design + streaming filter reproduce scipy's butter/sosfilt output.
    func testMatchesScipy() throws {
        let f = try fixture("filters.json") as! [String: Any]
        let x = f["x"] as! [Double]
        for c in f["cases"] as! [[String: Any]] {
            let fr = c["f"] as! [Double], fs = c["fs"] as! Double, n = c["order"] as! Int
            let kind: FilterKind = (c["kind"] as! String) == "bp" ? .bandpass(fr[0], fr[1]) : .highpass(fr[0])
            var flt = SOSFilter(Butterworth.design(order: n, kind, fs: fs), channels: 1)
            let want = c["y"] as! [Double]
            var maxErr = 0.0
            for (i, v) in x.enumerated() { maxErr = max(maxErr, abs(flt.step(v, channel: 0) - want[i])) }
            XCTAssertLessThan(maxErr, 1e-9, "\(c["kind"]!) \(fr) at \(fs) Hz")
        }
    }
}
