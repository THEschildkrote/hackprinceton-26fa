import Foundation
import SwiftUI

// spoken/visible metres: half-metre steps up close, whole metres beyond 10 (matches nudge/instruct.meters)
func fmtMetres(_ m: Double?) -> String {
    guard let m, !m.isNaN else { return "-" }
    if m < 0.75 { return "< 1 m" }
    let v = m < 10 ? (m * 2).rounded() / 2 : m.rounded()
    return v == v.rounded() ? "\(Int(v)) m" : String(format: "%.1f m", v)
}

// destination names from the place catalog: useful rooms first, corridors and closets last, no "Other".
// port of placeNames in mobile/src/lib/api.ts
enum PlaceOrder {
    static let first = ["bath", "restroom", "toilet", "kitchen", "living", "dining", "entr", "lobby", "reception",
                        "office", "conference", "meeting", "seminar", "atrium", "stair", "bedroom", "laundry", "elevator"]
    static let last = ["hall", "corridor", "closet", "storage"]
    static let hide = ["other", "void", "unknown"]

    static func rank(_ name: String) -> Int {
        let l = name.split(separator: ",").first.map(String.init)?.lowercased() ?? name.lowercased()
        if let i = first.firstIndex(where: { l.contains($0) }) { return i }
        return last.contains(where: { l.contains($0) }) ? 100 : 50
    }

    static func names(_ places: [Place]) -> [String] {
        let usable = places.filter { p in
            (p.kind == "room" || p.kind == "zone" || p.kind == "feature" || p.kind == "tenant")
                && !(p.unnamed ?? false)
                && !hide.contains(p.name.split(separator: ",").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? "")
        }
        let sorted = usable.sorted { a, b in
            let ka = a.kind == "feature" ? 1 : 0, kb = b.kind == "feature" ? 1 : 0
            if ka != kb { return ka < kb }
            let ra = rank(a.name), rb = rank(b.name)
            if ra != rb { return ra < rb }
            return a.name < b.name
        }
        var seen = Set<String>()
        return sorted.map(\.name).filter { seen.insert($0).inserted }
    }
}

// MARK: look and feel (red, white, blue, yellow)
//
// red, white, blue and yellow: the colours of Indonesia and the Philippines.
//
// contrast is the constraint, because some of the people who see this app have low vision.
// measured against WCAG: white on red 5.6, white on blue 9.9, yellow on blue 6.7, navy on yellow 10.5.
// yellow on white is 1.5 and fails, so yellow is never text on a light surface: it is a fill
// (with navy on top) or an icon on blue, navy or red. red text on white is fine (5.6).
//
// on the traveller's black screen it flips: yellow is the best colour there (14.3), white is
// 21, and red text is only 3.7, so red appears there only as a fill with white text on it.
//
// shared by both apps. it lives in Format.swift rather than a file of its own because the
// checked-in Xcode project lists files by hand: a new file would need registering, and this one
// already is, in every target that compiles Shared.
enum Theme {
    static let red = Color(hex: 0xCE1126)
    static let blue = Color(hex: 0x0038A8)
    static let navy = Color(hex: 0x0A1F5C)
    static let yellow = Color(hex: 0xFCD116)
    static let paper = Color(hex: 0xF7F4EC)   // warm white behind the cards
    static let mist = Color(hex: 0xE8EEF9)    // pale blue for tiles
    // a row sitting on navy: the same hue one step lighter, so a list reads as blue-on-blue.
    // the stock dark grey has no blue in it and looks pasted on.
    static let row = Color(hex: 0x1A3277)
    // the brand blue is only 2.1:1 on black, too dim for a ring or an icon there. this is the same
    // hue lifted until it reads: 8.1:1 on black, for the traveller's dark screen.
    static let sky = Color(hex: 0x6EA0FF)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

// a white card with a hairline and a soft shadow, instead of the stock grouped-list look
struct Card: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(.white, in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(Theme.navy.opacity(0.08), lineWidth: 1))
            .shadow(color: Theme.navy.opacity(0.08), radius: 10, y: 4)
    }
}

extension View {
    func card() -> some View { modifier(Card()) }
}

// the main action: yellow with navy text (10.5:1). still a real Button, so VoiceOver, Dynamic Type
// and the disabled state all come for free.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .foregroundStyle(enabled ? Theme.navy : .white.opacity(0.75))
            .frame(maxWidth: .infinity, minHeight: 56)
            // switched off reads as a ghost button, not a dim yellow (which goes olive on blue)
            .background(enabled ? Theme.yellow : .white.opacity(0.18), in: RoundedRectangle(cornerRadius: 18))
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.snappy(duration: 0.15), value: configuration.isPressed)
    }
}

// the blue bar that frames every screen
struct BrandBar: ViewModifier {
    func body(content: Content) -> some View {
        content
            .toolbarBackground(Theme.blue, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
    }
}

extension View {
    func brandBar() -> some View { modifier(BrandBar()) }
}
