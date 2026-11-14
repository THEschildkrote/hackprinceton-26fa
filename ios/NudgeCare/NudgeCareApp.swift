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
                .preferredColorScheme(.light)   // the palette's contrast was measured on light
        }
    }
}

// red, white, blue and yellow (indonesia and the philippines).
// measured contrast: white on red 5.6, red on white 5.6, white on blue 9.9, navy on yellow 10.5.
// yellow on white is 1.5, so yellow is only ever a fill with navy on top, never text or a lone icon.
// lives here because the checked-in xcode project lists files by hand.
enum Theme {
    static let red = Color(hex: 0xCE1126)
    static let blue = Color(hex: 0x0038A8)
    static let navy = Color(hex: 0x0A1F5C)
    static let yellow = Color(hex: 0xFCD116)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
