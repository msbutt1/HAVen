import SwiftUI

/// What the exposure score is, how it is calculated, and what it means (and does not mean).
struct OverviewView: View {
    @Environment(AppModel.self) private var backend

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Eyebrow(text: "Overview")
                Text("The exposure score").font(.title2.weight(.semibold))
                Text("A running total, for this shift, of how much vibration reached your forearm and how hard you were gripping while it did. It is experimental: a way to compare tasks and shifts for the same person, not a medical or legal measure.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }

            section("How it is calculated") {
                Text("Every moment the sleeve measures two things:").font(.subheadline)
                bullet("Vibration, V", "from the accelerometer on the forearm: the strength (RMS) of all shaking faster than 5 Hz over the last second, in m/s². Gravity and slow arm movement are filtered out.")
                bullet("Grip effort, a", "from the muscle sensor (EMG) on the forearm: 0 is your relaxed arm, 1 is your hardest squeeze from calibration.")
                Text("They are combined into a rate, and the rate is added up over time:").font(.subheadline)
                formula("rate (au per hour) = V² × (1 + a)")
                formula("score (au) = rate × hours")
                Text("V is in m/s². a is capped at 2 so a single spike cannot dominate. au means arbitrary units.")
                    .font(.footnote).foregroundStyle(Theme.muted)
                LiveExample()
                Text("Why squared: doubling the vibration counts four times as much, by analogy with vibration energy. Why grip: a tighter grip couples more of the tool's vibration into the hand and arm. Both are design choices of this prototype, not validated science.")
                    .font(.footnote).foregroundStyle(Theme.muted)
                Text("The score only counts while both signals are valid and fresh. When the sleeve is off, the data pauses, or grip is not calibrated, the score pauses: that time is never counted, and never guessed.")
                    .font(.footnote).foregroundStyle(Theme.muted)
            }

            section("The bar") {
                Text("The dashboard bar shows this shift's score against a reference of \(formatNumber(backend.exposureReference, 0)) au per shift. That is what the formula gives for 8 hours at a vibration of 2.5 m/s² with 50 % grip effort (6.25 × 1.5 × 8 = 75).")
                    .font(.subheadline)
                HStack(spacing: 12) {
                    legend(Theme.success, "under 50 %")
                    legend(Theme.warning, "50–100 %")
                    legend(Theme.destructive, "over 100 %")
                }
                Text("The reference is a team setting for the prototype. It is not a legal limit, and 100 % does not mean harm. New shift sets the score and tool time back to zero.")
                    .font(.footnote).foregroundStyle(Theme.muted)
            }

            section("What it means") {
                bullet("Higher means more", "more vibration, for longer, with a harder grip. A task that fills the bar faster is the one to look at first.")
                bullet("Compare like with like", "the same person, the same sleeve placement and the same calibration: grip effort is relative to your own squeeze.")
                bullet("What it is not", "not a vibration dose, not A(8) or any standard exposure value, not a percentage of a legal limit, and not a diagnosis. The vibration is unweighted and measured by a hobby-grade sensor on the forearm, not at the hand as standards require.")
            }

            section("Tool use") {
                Text("Separately, the dashboard shows whether a power tool is in use, and the tool time this shift. A tool counts only when the vibration is fast (20 Hz and up), strong compared with arm movement, periodic, and steady for 2 seconds, so handshakes, waving and knocks do not count. Calibrating with your own tool sets these thresholds for you. Warnings after 20 minutes of continuous use and at 2 hours a shift are team settings, not regulatory limits.")
                    .font(.subheadline)
            }

            Text("Everything is calculated on this phone, live, from the sleeve's raw signals.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
    }

    private func section<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .panel()
    }

    private func bullet(_ title: String, _ text: String) -> some View {
        (Text(title + ": ").fontWeight(.semibold) + Text(text)).font(.subheadline)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func formula(_ s: String) -> some View {
        Text(s).font(.body.monospaced().weight(.medium))
            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.section))
    }

    private func legend(_ c: Color, _ t: String) -> some View {
        HStack(spacing: 6) { Capsule().fill(c).frame(width: 18, height: 8); Text(t).font(.caption) }
    }
}

/// The formula with this moment's numbers.
struct LiveExample: View {
    @Environment(AppModel.self) private var backend

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            let fresh = backend.freshness(at: ctx.date) == .fresh
            let t = fresh ? backend.latest : nil
            let a = t?.activationPct == nil ? nil : backend.snapshot.activation.map { min(2, max(0, $0)) }   // as the score uses it
            VStack(alignment: .leading, spacing: 4) {
                Text("Right now").font(.caption.weight(.semibold)).foregroundStyle(Theme.muted)
                if let v = t?.vibration, let a, let rate = backend.snapshot.scoreRate {
                    Text("\(formatNumber(v, 2))² × (1 + \(formatNumber(a, 2))) = \(formatScore(rate)) au per hour")
                        .font(.subheadline.monospacedDigit())
                    Text("At this rate one hour adds \(formatScore(rate)) au, \(formatNumber(rate / max(backend.exposureReference, 1e-9) * 100, 1)) % of the shift reference.")
                        .font(.caption).foregroundStyle(Theme.muted)
                } else {
                    Text(t == nil ? "No live data. Start the sleeve to see the formula with your numbers."
                         : "Calibrate grip on the dashboard to see the formula with your numbers.")
                        .font(.subheadline).foregroundStyle(Theme.muted)
                }
            }
            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.border))
        }
    }
}
