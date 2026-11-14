import SwiftUI

@main
struct NudgeApp: App {
    @State private var settings = TravellerSettings()

    var body: some Scene {
        WindowGroup {
            TravellerRoot()
                .environment(settings)
        }
    }
}
