import SwiftUI
import UIKit

/// The landing page's design system (livops.uz's tokens, light and dark), converted from OKLCH to sRGB.
enum Theme {
    private static func dynamic(_ light: UInt32, _ dark: UInt32, darkAlpha: CGFloat = 1) -> Color {
        Color(UIColor { t in
            t.userInterfaceStyle == .dark ? UIColor(hex: dark, alpha: darkAlpha) : UIColor(hex: light)
        })
    }

    static let background = dynamic(0xffffff, 0x0c0f15)
    static let foreground = dynamic(0x0b0f19, 0xeef0f4)
    static let card = dynamic(0xfafbfc, 0x151921)
    static let cardTop = dynamic(0xfafbfc, 0x1c2028)
    static let section = dynamic(0xf3f4f7, 0x10141a)
    static let muted = dynamic(0x666d7c, 0x99a0af)
    static let border = dynamic(0xdfe2e8, 0xffffff, darkAlpha: 0.11)
    static let rim = dynamic(0xdfe2e8, 0xffffff, darkAlpha: 0.22)
    static let primary = dynamic(0x006ae6, 0x4495ff)
    static let primaryForeground = dynamic(0xfbfcfd, 0x060d1a)
    static let success = dynamic(0x008140, 0x3eb268)
    static let successForeground = dynamic(0xfbfdfc, 0x041107)
    static let warning = dynamic(0xe4960e, 0xeda839)
    static let destructive = dynamic(0xe30008, 0xff6468)
    static let sim = dynamic(0x7a4aba, 0x8962c5)
    static let sim2 = dynamic(0x673ba2, 0x714ca6)
    static let activation = primary
    static let vibration = dynamic(0xcc6600, 0xf59740)
    static let score = dynamic(0x008774, 0x39c3ae)

    static let radius: CGFloat = 15
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
                  blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
    }
}

/// The rim-lit card: a faint top highlight and a brighter top edge, like the landing page's `.panel`.
struct Panel: ViewModifier {
    var dashed = false
    var padding: CGFloat = 16

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LinearGradient(colors: [Theme.cardTop, Theme.card], startPoint: .top, endPoint: .center), in: shape)
            .overlay {
                if dashed {
                    shape.strokeBorder(Theme.muted.opacity(0.55), style: StrokeStyle(lineWidth: 1, dash: [6, 5]))
                } else {
                    shape.strokeBorder(LinearGradient(colors: [Theme.rim, Theme.border], startPoint: .top, endPoint: .center), lineWidth: 1)
                }
            }
    }
}

extension View {
    func panel(dashed: Bool = false, padding: CGFloat = 16) -> some View { modifier(Panel(dashed: dashed, padding: padding)) }
}

/// Small uppercase mono label above a heading ("WHAT IT MEASURES").
struct Eyebrow: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .medium, design: .monospaced))
            .tracking(1.6)
            .foregroundStyle(Theme.muted)
    }
}

/// Diagonal stripes, used for anything simulated (the same pattern as the web dashboard).
struct Stripes: View {
    var width: CGFloat = 10
    var body: some View {
        Canvas { ctx, size in
            ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Theme.sim2))
            var x: CGFloat = -size.height
            while x < size.width + size.height {
                var p = Path()
                p.move(to: CGPoint(x: x, y: size.height))
                p.addLine(to: CGPoint(x: x + width, y: size.height))
                p.addLine(to: CGPoint(x: x + width + size.height, y: 0))
                p.addLine(to: CGPoint(x: x + size.height, y: 0))
                p.closeSubpath()
                ctx.fill(p, with: .color(Theme.sim))
                x += width * 2
            }
        }
    }
}

/// Live / Replay / Simulated, the same labels as the dashboard and the landing page.
struct SourceTag: View {
    let source: Provenance
    var body: some View {
        Text(source.label)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 9)
            .padding(.vertical, 3)
            .foregroundStyle(source == .simulated ? Color.white : (source == .live ? Theme.successForeground : Theme.primaryForeground))
            .background {
                switch source {
                case .live: Capsule().fill(Theme.success)
                case .replay: Capsule().fill(Theme.primary)
                case .simulated: Stripes(width: 5).clipShape(Capsule())
                }
            }
    }
}

struct ExperimentalTag: View {
    var body: some View {
        Text("Experimental")
            .font(.caption2.weight(.medium))
            .foregroundStyle(Theme.muted)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .overlay(Capsule().strokeBorder(Theme.muted, style: StrokeStyle(lineWidth: 1, dash: [3, 2])))
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    var role: ButtonRole?
    var big = false
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        let destructive = role == .destructive
        configuration.label
            .font(big ? .title3.weight(.semibold) : .body.weight(.medium))
            .frame(maxWidth: big ? .infinity : nil)
            .padding(.horizontal, 18)
            .frame(minHeight: big ? 72 : 44)
            .foregroundStyle(destructive ? Color.white : Theme.primaryForeground)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(destructive ? Theme.destructive : Theme.primary))
            .opacity(enabled ? (configuration.isPressed ? 0.85 : 1) : 0.45)
    }
}

struct OutlineButtonStyle: ButtonStyle {
    var big = false
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(big ? .title3.weight(.semibold) : .body.weight(.medium))
            .frame(maxWidth: big ? .infinity : nil)
            .padding(.horizontal, 18)
            .frame(minHeight: big ? 72 : 44)
            .foregroundStyle(Theme.foreground)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Theme.section.opacity(configuration.isPressed ? 1 : 0.6)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Theme.border))
            .opacity(enabled ? 1 : 0.45)
    }
}

/// The HAVen mark: a blue rounded square with a signal trace.
struct LogoMark: View {
    var size: CGFloat = 28
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(Theme.primary)
            .frame(width: size, height: size)
            .overlay {
                Canvas { ctx, s in
                    let pts: [CGPoint] = [(3, 12), (7, 12), (9, 7), (12, 17), (15, 10), (17, 12), (21, 12)]
                        .map { CGPoint(x: $0.0 / 24 * s.width, y: $0.1 / 24 * s.height) }
                    var p = Path()
                    p.addLines(pts)
                    ctx.stroke(p, with: .color(Theme.primaryForeground),
                               style: StrokeStyle(lineWidth: s.width * 0.1, lineCap: .round, lineJoin: .round))
                }
                .padding(size * 0.12)
            }
            .accessibilityHidden(true)
    }
}
