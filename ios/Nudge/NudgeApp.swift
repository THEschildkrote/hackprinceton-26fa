import SwiftUI

@main
struct NudgeApp: App {
    @State private var settings = TravellerSettings()

    var body: some Scene {
        WindowGroup {
            TravellerRoot()
                .environment(settings)
                .tint(Theme.yellow)   // 14.3:1 on black; the app is always dark
        }
    }
}
