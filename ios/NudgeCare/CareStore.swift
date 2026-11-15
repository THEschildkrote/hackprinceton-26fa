import Foundation
import Observation
import UserNotifications

// everything the caretaker app knows: who they follow, the live status, the message feed, and alerts.
@MainActor
@Observable
final class CareStore {
    private let d = UserDefaults.standard

    var server: String { didSet { d.set(server, forKey: "care.server") } }
    var callNumber: String { didSet { d.set(callNumber, forKey: "care.call") } }
    private(set) var travellerId: String? { didSet { d.set(travellerId, forKey: "care.traveller") } }
    private(set) var travellerName: String? { didSet { d.set(travellerName, forKey: "care.name") } }

    private(set) var status: TravellerStatus?
    private(set) var messages: [CareMessage] = []
    private(set) var alert: CareMessage?          // the latest urgent message, until dismissed
    private(set) var reachable = true
    private(set) var plans: [String: FloorPlan] = [:]
    private(set) var tripSentTo: String?

    private var pollTask: Task<Void, Never>?
    private var streamTask: Task<Void, Never>?
    private var seen = Set<String>()

    init() {
        server = d.string(forKey: "care.server") ?? "http://172.20.10.2:8765"
        callNumber = d.string(forKey: "care.call") ?? ""
        travellerId = d.string(forKey: "care.traveller")
        travellerName = d.string(forKey: "care.name")
    }

    var api: API { API(base: server) }
    var paired: Bool { travellerId != nil }
    var displayName: String { travellerName.flatMap { $0.isEmpty ? nil : $0 } ?? status?.name ?? "Your traveller" }

    // MARK: pairing

    func pair(code: String) async throws {
        let digits = code.filter(\.isNumber)
        let r = try await api.pair(code: digits)
        travellerId = r.traveller_id
        travellerName = r.name
        messages = []
        seen = []
        start()
    }

    func unpair() {
        stop()
        travellerId = nil
        travellerName = nil
        status = nil
        messages = []
    }

    // MARK: live updates

    func start() {
        guard let id = travellerId else { return }
        Task { _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) }
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh(id)
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        streamTask?.cancel()
        streamTask = Task { [weak self] in
            // the event stream drops on flaky wifi; reconnect, and the 2 s status poll covers the gap
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    for try await m in self.api.messages(traveller: id) { self.receive(m) }
                } catch {}
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        streamTask?.cancel()
    }

    private func refresh(_ id: String) async {
        do {
            let s = try await api.status(traveller: id)
            reachable = true
            if s != status { status = s }
            if let sid = s.sid ?? s.position?.sid, plans[sid] == nil {
                if let p = try? await api.floorPlan(sid: sid) { plans[sid] = p }
            }
            if s.state == "walking" || s.state == "arrived" { tripSentTo = nil }
        } catch {
            reachable = false
        }
    }

    private func receive(_ m: CareMessage) {
        guard seen.insert(m.id).inserted else { return }
        messages.insert(m, at: 0)
        if m.urgent == true {
            alert = m
            notify(m.text)
        }
    }

    func dismissAlert() { alert = nil }

    // the traveller is standing on the next corner of their route: tell the belt, so a drifted position is put right
    func atCorner() async -> Bool {
        (try? await api.beltMark()) ?? false
    }

    private func notify(_ text: String) {
        let c = UNMutableNotificationContent()
        c.title = "\(displayName) needs attention"
        c.body = text
        c.sound = .defaultCritical
        c.interruptionLevel = .timeSensitive
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    // MARK: trips

    func sendTrip(sid: String, to: String, profile: String) async throws {
        guard let id = travellerId else { return }
        try await api.sendTrip(traveller: id, sid: sid, to: to, profile: profile)
        tripSentTo = to
    }
}

// show notifications as banners even while the app is open
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }
}
