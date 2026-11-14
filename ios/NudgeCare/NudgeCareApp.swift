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
                .tint(.indigo)
        }
    }
}
