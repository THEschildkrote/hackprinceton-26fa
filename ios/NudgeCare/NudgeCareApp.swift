import SwiftUI
import UserNotifications

@main
struct NudgeCareApp: App {
    @State private var store = CareStore()
    private let notifications = NotificationDelegate()

    init() {
        UNUserNotificationCenter.current().delegate = notifications
    }

    var body: some Scene {
        WindowGroup {
            CareRoot()
                .environment(store)
                .tint(Theme.blue)
                // one branded look. the palette is tuned for light; see the contrast notes below.
                .preferredColorScheme(.light)
        }
    }
}

// MARK: look and feel
//
// red, white, blue and yellow: the colours of Indonesia and the Philippines.
//
// contrast is the constraint, because some of the people who see this app have low vision.
// measured against WCAG: white on red 5.6, white on blue 9.9, yellow on blue 6.7, navy on yellow 10.5.
// yellow on white is 1.5 and fails, so yellow is never text on a light surface: it is a fill
// (with navy on top) or an icon on blue, navy or red. red text on white is fine (5.6).
//
// this lives here rather than in its own file because the checked-in Xcode project lists files
// by hand; a new file would need registering, and this one already is.
enum Theme {
    static let red = Color(hex: 0xCE1126)
    static let blue = Color(hex: 0x0038A8)
    static let navy = Color(hex: 0x0A1F5C)
    static let yellow = Color(hex: 0xFCD116)
    static let paper = Color(hex: 0xF7F4EC)   // warm white behind the cards
    static let mist = Color(hex: 0xE8EEF9)    // pale blue for tiles
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
