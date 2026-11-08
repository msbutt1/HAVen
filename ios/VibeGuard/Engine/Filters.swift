import Foundation

// On-phone signal processing for the VibeGuard sleeve. This folder is a Swift port of
// signals/src/vibeguard_signals/processing (the Python processor). It uses Foundation only,
// so it also builds and is tested on Linux (ios/Package.swift, ios/EngineTests), against
// outputs of the Python processor on the same recordings.

/// Complex number (just what the filter design needs).
struct Cx {
    var re: Double
    var im: Double
    init(_ re: Double, _ im: Double = 0) { self.re = re; self.im = im }
    static func + (a: Cx, b: Cx) -> Cx { Cx(a.re + b.re, a.im + b.im) }
    static func - (a: Cx, b: Cx) -> Cx { Cx(a.re - b.re, a.im - b.im) }
    static func * (a: Cx, b: Cx) -> Cx { Cx(a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re) }
    static func * (a: Cx, s: Double) -> Cx { Cx(a.re * s, a.im * s) }
    static func / (a: Cx, b: Cx) -> Cx {
        let d = b.re * b.re + b.im * b.im
        return Cx((a.re * b.re + a.im * b.im) / d, (a.im * b.re - a.re * b.im) / d)
    }
    var sqrt: Cx {
        let r = (re * re + im * im).squareRoot()
        let s = ((r + re) / 2).squareRoot()
        let t = ((r - re) / 2).squareRoot()
        return Cx(s, im < 0 ? -t : t)
    }
}

/// One second-order section: b0 b1 b2 / 1 a1 a2 (scipy's sos row with a0 = 1).
struct Biquad {
    var b0, b1, b2, a1, a2: Double
}

enum FilterKind { case lowpass(Double), highpass(Double), bandpass(Double, Double) }

/// Digital Butterworth design, the same steps as scipy.signal.butter(..., output="sos"):
/// analog prototype, pre-warp, frequency transform, bilinear transform, second-order sections.
enum Butterworth {
    static func design(order n: Int, _ kind: FilterKind, fs: Double) -> [Biquad] {
        // analog prototype poles (buttap)
        var poles: [Cx] = []
        for m in stride(from: -n + 1, to: n, by: 2) {
            let a = Double.pi * Double(m) / Double(2 * n)
            poles.append(Cx(-cos(a), -sin(a)))   // -exp(j*pi*m/(2n))
        }
        var zeros: [Cx] = []
        var k = 1.0
        let fs2 = 2.0                                   // scipy designs with fs = 2 after normalising
        func warp(_ f: Double) -> Double { 2 * fs2 * tan(Double.pi * (f / (fs / 2)) / fs2) }
        switch kind {
        case .lowpass(let f):
            let wo = warp(f)
            poles = poles.map { $0 * wo }
            k *= pow(wo, Double(n))
        case .highpass(let f):
            let wo = warp(f)
            var prodP = Cx(1)
            for p in poles { prodP = prodP * (Cx(0) - p) }
            k *= (Cx(1) / prodP).re                      // prod(-z)/prod(-p) with no zeros
            poles = poles.map { Cx(wo) / $0 }
            zeros = Array(repeating: Cx(0), count: n)
        case .bandpass(let f1, let f2):
            let w1 = warp(f1), w2 = warp(f2)
            let bw = w2 - w1, wo = (w1 * w2).squareRoot()
            var out: [Cx] = []
            for p in poles {
                let pl = p * (bw / 2)
                let r = (pl * pl - Cx(wo * wo)).sqrt
                out.append(pl + r)
                out.append(pl - r)
            }
            poles = out
            zeros = Array(repeating: Cx(0), count: n)
            k *= pow(bw, Double(n))
        }
        // bilinear transform (fs = 2): s -> 2*fs*(z-1)/(z+1)
        let f4 = 2 * fs2
        var num = Cx(1), den = Cx(1)
        for z in zeros { num = num * (Cx(f4) - z) }
        for p in poles { den = den * (Cx(f4) - p) }
        k *= (num / den).re
        let zz = zeros.map { (Cx(f4) + $0) / (Cx(f4) - $0) } + Array(repeating: Cx(-1), count: poles.count - zeros.count)
        let pz = poles.map { (Cx(f4) + $0) / (Cx(f4) - $0) }
        return sections(zeros: zz, poles: pz, gain: k)
    }

    /// Pair conjugate poles and real zeros into biquads (all Butterworth zeros are real: +1 or -1).
    private static func sections(zeros: [Cx], poles: [Cx], gain: Double) -> [Biquad] {
        var cplx = poles.filter { $0.im > 1e-12 }
        var real = poles.filter { abs($0.im) <= 1e-12 }.map(\.re)
        var zr = zeros.map(\.re).sorted()
        var out: [Biquad] = []
        // closest-to-unit-circle poles last, like scipy, for numerical behaviour
        cplx.sort { hypot($0.re, $0.im) < hypot($1.re, $1.im) }
        real.sort { abs($0) < abs($1) }
        func takeZeros(_ count: Int) -> (Double, Double) {
            // returns (b1, b2) for (1 - z1 q)(1 - z2 q), q = z^-1
            var zs: [Double] = []
            for _ in 0..<count where !zr.isEmpty { zs.append(zr.removeFirst()) }
            switch zs.count {
            case 2: return (-(zs[0] + zs[1]), zs[0] * zs[1])
            case 1: return (-zs[0], 0)
            default: return (0, 0)
            }
        }
        for p in cplx {
            let (b1, b2) = takeZeros(2)
            out.append(Biquad(b0: 1, b1: b1, b2: b2, a1: -2 * p.re, a2: p.re * p.re + p.im * p.im))
        }
        while !real.isEmpty {
            let p1 = real.removeFirst()
            if let p2 = real.first {
                real.removeFirst()
                let (b1, b2) = takeZeros(2)
                out.append(Biquad(b0: 1, b1: b1, b2: b2, a1: -(p1 + p2), a2: p1 * p2))
            } else {
                let (b1, b2) = takeZeros(1)
                out.append(Biquad(b0: 1, b1: b1, b2: b2, a1: -p1, a2: 0))
            }
        }
        if !out.isEmpty {
            out[0].b0 *= gain; out[0].b1 *= gain; out[0].b2 *= gain
        }
        return out
    }
}

/// Streaming cascade of biquads for several channels, transposed direct form II (as scipy sosfilt),
/// state carried across batches, starting from zero state.
struct SOSFilter {
    let sections: [Biquad]
    private var z: [[Double]]           // per channel: 2 state values per section

    init(_ sections: [Biquad], channels: Int) {
        self.sections = sections
        z = Array(repeating: Array(repeating: 0, count: 2 * sections.count), count: channels)
    }

    mutating func reset() {
        for c in z.indices { for i in z[c].indices { z[c][i] = 0 } }
    }

    mutating func step(_ x: Double, channel c: Int) -> Double {
        var v = x
        for (i, s) in sections.enumerated() {
            let y = s.b0 * v + z[c][2 * i]
            z[c][2 * i] = s.b1 * v - s.a1 * y + z[c][2 * i + 1]
            z[c][2 * i + 1] = s.b2 * v - s.a2 * y
            v = y
        }
        return v
    }
}

/// Numerics helpers that match numpy.
enum NP {
    /// numpy.percentile, linear interpolation.
    static func percentile(_ values: [Double], _ p: Double) -> Double {
        guard !values.isEmpty else { return .nan }
        let s = values.sorted()
        let pos = p / 100 * Double(s.count - 1)
        let lo = Int(pos.rounded(.down)), hi = min(lo + 1, s.count - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - Double(lo))
    }
    static func median(_ v: [Double]) -> Double { percentile(v, 50) }
    /// Python round(x, d): round half to even.
    static func round(_ x: Double, _ d: Int) -> Double {
        let m = pow(10, Double(d))
        return (x * m).rounded(.toNearestOrEven) / m
    }
}
