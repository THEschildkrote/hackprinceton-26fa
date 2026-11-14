import Foundation
import Observation

// what the traveller's phone remembers, in userdefaults
@Observable
final class TravellerSettings {
    private let d = UserDefaults.standard

    var server: String { didSet { d.set(server, forKey: "server") } }
    var stepM: Double { didSet { d.set(stepM, forKey: "stepM") } }
    var mountRaw: String { didSet { d.set(mountRaw, forKey: "mount") } }
    var name: String { didSet { d.set(name, forKey: "name") } }
    var traveller: String { didSet { d.set(traveller, forKey: "traveller") } }
    var code: String? { didSet { d.set(code, forKey: "code") } }
    var setupDone: Bool { didSet { d.set(setupDone, forKey: "setupDone") } }
    var lastTripData: Data? { didSet { d.set(lastTripData, forKey: "lastTrip") } }

    init() {
        // on an iphone hotspot the pi is usually 172.20.10.2-14
        server = d.string(forKey: "server") ?? "http://172.20.10.2:8765"
        let s = d.double(forKey: "stepM")
        stepM = s > 0 ? s : 0.7
        mountRaw = d.string(forKey: "mount") ?? Mount.screenFacesBody.rawValue
        name = d.string(forKey: "name") ?? ""
        if let t = d.string(forKey: "traveller") {
            traveller = t
        } else {
            let t = "t-" + UUID().uuidString.prefix(8).lowercased()
            traveller = t
            d.set(t, forKey: "traveller")
        }
        code = d.string(forKey: "code")
        setupDone = d.bool(forKey: "setupDone")
        lastTripData = d.data(forKey: "lastTrip")
    }

    var api: API { API(base: server) }
    var mount: Mount { Mount(rawValue: mountRaw) ?? .screenFacesBody }

    var lastTrip: Trip? {
        get { lastTripData.flatMap { try? JSONDecoder().decode(Trip.self, from: $0) } }
        set { lastTripData = newValue.flatMap { try? JSONEncoder().encode($0) } }
    }
}
