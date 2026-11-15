import Foundation
import Observation
import UIKit

// the traveller's phone. a trip starts one of two ways: they tap anywhere, which calls isaac's line, and say
// where they want to go (with no line set the tap is tap-to-talk instead), or their caretaker sends one.
// either way the caretaker can follow it.
// calibration is a spoken countdown, guidance is spoken, the belt taps, and a tap lets them talk back
// ("where am I", "the door is locked", "yes").
@MainActor
@Observable
final class Traveller {
    enum Phase: Equatable { case waiting, starting, calibrating, walking, arrived, stopped }

    let settings: TravellerSettings
    private(set) var phase: Phase = .waiting
    private(set) var status = "Tap anywhere to call Isaac."
    private(set) var trip: Trip?
    private(set) var session: String?
    private(set) var heading: Double?
    private(set) var compassPoor = false
    private(set) var piReachable = true
    private(set) var distanceToNext: Double?
    private(set) var heard = ""
    private(set) var listening = false
    private var micAllowed = false
    private var pendingApproval = false
    private var lastSaid = ""

    private var queue = StepQueue()
    var steps: Int { queue.total }

    private let motion = Motion()
    private let listener = Listener()
    private var pollTask: Task<Void, Never>?
    private var idleTask: Task<Void, Never>?
    private var posting = false
    private var lastPost = Date.distantPast

    init(settings: TravellerSettings) {
        self.settings = settings
        listener.onListening = { [weak self] on in
            self?.listening = on
            if !on { Speech.shared.holding = false }   // say what came in while they talked
        }
        listener.onUtterance = { [weak self] text in Haptic.tick(); Task { await self?.command(text) } }
        listener.onNothingHeard = { [weak self] in self?.say("I didn't catch that. Tap and try again.") }
        motion.onHeading = { [weak self] deg, poor in self?.heading = deg; self?.compassPoor = poor }
        motion.onStep = { [weak self] in self?.onStep() }
    }

    // MARK: waiting for a trip from the caretaker

    func begin() async {
        // keep running with the screen on but dark; background audio keeps it alive if the screen locks
        UIApplication.shared.isIdleTimerDisabled = true
        Speech.shared.configureAudio()
        micAllowed = await Listener.requestAccess()   // asked once; the mic stays off until a tap
        motion.mount = settings.mount
        motion.start()   // heading is ready by the time a trip arrives
        // trips are looked up by traveller id, which the pi only knows once we've registered
        if settings.code == nil,
           let r = try? await settings.api.register(traveller: settings.traveller, name: settings.name) {
            settings.code = r.code
        }
        say("Isaac is ready. Tap the screen to call Isaac and say where you want to go.")
        startPolling()
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.phase == .waiting || self.phase == .arrived || self.phase == .stopped {
                    if let t = try? await self.settings.api.pendingTrip(traveller: self.settings.traveller,
                                                                       heading: self.heading) {
                        await self.start(t, fromCaretaker: true)
                    }
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    // MARK: tap to talk

    var onTrip: Bool { phase == .starting || phase == .calibrating || phase == .walking }

    // the whole screen. off a trip it calls isaac's line: the agent on the call plans the trip and it lands
    // here like a caretaker's. on a trip (or with no line set) it opens the mic for one sentence
    func tap() {
        if !onTrip, !listener.listening, callIsaac() { return }
        talk()
    }

    // false when there's no line set or the phone can't call (a simulator, an ipad): fall back to talking
    @discardableResult
    func callIsaac() -> Bool {
        let digits = settings.isaacLine.filter(\.isNumber)
        // no canOpenURL check: it needs the scheme declared in Info.plist. open() reports failure instead
        guard digits.count >= 10, let url = URL(string: "tel://\(digits)") else { return false }
        Speech.shared.hush()
        status = "Calling Isaac."
        Haptic.tick()
        UIApplication.shared.open(url) { [weak self] ok in
            Task { @MainActor in
                guard let self else { return }
                if !ok { self.status = "Tap anywhere and say where to go."; self.talk(); return }
                // the trip arrives through the usual poll once the agent has it
                self.status = "Waiting for your trip from the call."
            }
        }
        return true
    }

    // a tap opens the mic for one sentence. tapping again while it listens sends it now
    func talk() {
        if listener.listening { listener.finishNow(); return }
        guard micAllowed else {
            say("Isaac can't use the microphone. Turn on the microphone and speech recognition in Settings.")
            return
        }
        Speech.shared.hush()
        Speech.shared.holding = true
        if listener.listen() {
            heard = ""
            Haptic.tick()
        } else {
            Speech.shared.holding = false
            say("Voice isn't ready yet. Try again in a moment.")
        }
    }

    // "start my trip": run the last trip again
    func startLastTrip() async {
        guard let t = settings.lastTrip else {
            say("No trip yet. Tap and say where you want to go.")
            return
        }
        await start(t, fromCaretaker: false)
    }

    // MARK: start and calibrate

    func start(_ t: Trip, fromCaretaker: Bool) async {
        guard phase != .starting, phase != .calibrating, phase != .walking else { return }
        phase = .starting
        trip = t
        settings.lastTrip = t
        motion.mount = settings.mount   // setup may have changed how the phone is worn
        try? await settings.api.beltWearer(heightCm: settings.heightCm)   // the belt steps to the wearer's height
        queue = StepQueue()
        status = "Starting a trip to \(t.to)."
        say(fromCaretaker && t.by != "walker" ? "Your caretaker sent a trip to \(t.to)." : "Starting your trip to \(t.to).")
        do {
            let r = try await settings.api.start(sid: t.sid, to: t.to, profile: t.profile, traveller: settings.traveller)
            session = r.session
            piReachable = true
            if fromCaretaker { try? await settings.api.ackTrip(traveller: settings.traveller) }
            say(r.events.compactMap(\.text))
            await calibrate()
        } catch {
            if fromCaretaker { try? await settings.api.ackTrip(traveller: settings.traveller) }
            phase = .waiting
            status = "Couldn't start the trip."
            say(message(for: error))
        }
    }

    // hands-free: count down while they stand at the entrance facing the way they'll walk
    private func calibrate() async {
        phase = .calibrating
        status = "Calibrating."
        say("Stand at the entrance facing the way you'll walk. Calibrating in 3, 2, 1.")
        try? await Task.sleep(nanoseconds: 5_500_000_000)
        guard let session else { return }
        guard let h = heading else {
            say("I can't find a direction. Keep the phone upright on your back.")
            phase = .waiting
            status = "No direction yet. Waiting for a trip."  // otherwise "Calibrating." stays on screen
            return
        }
        do {
            _ = try await settings.api.calibrate(session: session, headingDeg: h)
            Haptic.success()
            say("Go ahead.")
            phase = .walking
            status = "Walking to \(trip?.to ?? "your destination")."
            startIdleUpdates()
        } catch {
            handle(error)
        }
    }

    // MARK: walking

    private func onStep() {
        guard phase == .walking else { return }
        queue.add()
        Task { await post() }
    }

    // a heading-only update every second while standing still, so turning in place registers
    private func startIdleUpdates() {
        idleTask?.cancel()
        idleTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard let self, self.phase == .walking else { continue }
                if Date().timeIntervalSince(self.lastPost) >= 1 { await self.post() }
            }
        }
    }

    private func post() async {
        guard let session, phase == .walking, !posting else { return }
        posting = true
        defer { posting = false }
        let n = queue.take()
        lastPost = Date()
        do {
            let r = try await settings.api.step(session: session, steps: n, headingDeg: heading, stepM: settings.stepM)
            queue.ack()
            if !piReachable { say("Back in touch.") }
            piReachable = true
            say(r.say ?? [])
            if r.ask == "stall" { pendingApproval = true }   // so a plain yes or no reaches the pi
            distanceToNext = r.distance_to_next_m ?? r.state?.distance_to_next_m
            if r.done == true || r.state?.done == true {
                phase = .arrived
                status = "Arrived at \(trip?.to ?? "your destination")."
                idleTask?.cancel()
                Haptic.success()
            }
        } catch {
            queue.fail()   // never lose steps: they go out with the next request
            handle(error)
        }
        if queue.pending > 0, phase == .walking { Task { await post() } }
    }

    // MARK: voice commands

    func command(_ raw: String) async {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        heard = text
        let low = text.lowercased()
        if low == "repeat" || low.hasPrefix("repeat") || low == "say that again" {
            Speech.shared.say(lastSaid.isEmpty ? "Nothing to repeat yet." : lastSaid, interrupt: true)
            return
        }
        if low == "stop" || low.hasPrefix("stop navigation") || low == "cancel trip" {
            stopTrip()
            return
        }
        // stairs or a lift: the belt stops at a change of floor and waits to be told they are on the next one
        if low.contains("upstairs") || low.contains("downstairs") || low.contains("next floor") {
            let ok = (try? await settings.api.beltFloor()) ?? false
            say(ok ? "Okay, carrying on from here." : "There is no next floor on this route.")
            return
        }
        if (low == "start" || low.contains("start my trip")) && phase != .walking {
            await startLastTrip()
            return
        }
        // a yes/no only means something when the pi is waiting for an answer
        let isAnswer = ["yes", "no", "yeah", "nope", "okay", "ok"].contains(low)
        if isAnswer && !pendingApproval { return }
        // everything else (where am i, how far, help, the door is locked) goes to the pi's voice brain
        do {
            let r = try await settings.api.voiceTurn(traveller: settings.traveller, text: text,
                                                     session: session, sid: trip?.sid)
            if let now = r.say_now, !now.isEmpty { say(now) } else if let s = r.say { say(s) }
            var reply = r
            if let job = r.pending_job {
                reply = try await poll(job)
                if let s = reply.say, !s.isEmpty { say(s) }
            }
            pendingApproval = reply.pending != nil
            // "take me to the kitchen" starts a trip on the pi and names its session in the reply.
            // take it up here, or no steps are ever sent for it and nothing guides.
            // a finished trip is not taken up: after a restart the app has no session, and "where am i"
            // would otherwise name the pi's last, already-arrived one and start a countdown for it
            if let s = reply.session ?? r.session, s != session, (reply.state ?? r.state)?.done != true {
                await adopt(s, trip: reply.trip ?? r.trip)
            }
        } catch {
            // a spoken command never goes unanswered: handle() only speaks the first time the server is lost
            let wasReachable = piReachable
            handle(error)
            if !wasReachable, !((error as? APIError)?.isUnknownSession ?? false) {
                say("I can't reach the server. Check that the phone is on the hotspot.")
            }
        }
    }

    // a trip started by voice: the pi has already planned it and said where it goes
    private func adopt(_ newSession: String, trip voiceTrip: VoiceTrip? = nil) async {
        guard phase != .starting, phase != .calibrating else { return }
        let wasWalking = phase == .walking
        session = newSession
        queue = StepQueue()
        pendingApproval = false
        if let sid = voiceTrip?.sid, let goal = voiceTrip?.goal {
            // so the screen, "start my trip" and the next voice turn all know where this one goes
            let t = Trip(sid: sid, to: goal, profile: "blind", ts: nil)
            trip = t
            settings.lastTrip = t
        }
        if wasWalking {
            // asked mid-walk: the direction is already known, so keep walking and report to the new trip
            status = "Walking to your new destination."
            return
        }
        motion.mount = settings.mount
        phase = .starting
        status = "Starting your trip."
        await calibrate()
    }

    // the agent can take a while; the pi falls back to a safe reroute after ~20 s
    private func poll(_ job: String) async throws -> VoiceReply {
        for _ in 0..<45 {
            try await Task.sleep(nanoseconds: 1_000_000_000)
            let r = try await settings.api.voiceJob(job)
            if r.done == true { return r }
        }
        return VoiceReply(say: "Still working on it. Stay where you are.", say_now: nil, pending_job: nil,
                          done: true, session: nil, pending: nil, state: nil)
    }

    // MARK: stop

    func stopTrip() {
        idleTask?.cancel()
        phase = .stopped
        status = "Trip stopped. Waiting for a trip."
        say("Trip stopped.")
        let api = settings.api
        Task { try? await api.beltStop() }   // otherwise the belt taps on until its sensor goes quiet
    }

    // MARK: helpers

    private func say(_ lines: [String]) { for l in lines { say(l) } }

    private func say(_ line: String) {
        let t = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        lastSaid = t
        Speech.shared.say(t)
    }

    private func handle(_ error: Error) {
        if let e = error as? APIError, e.isUnknownSession {
            idleTask?.cancel()
            phase = .waiting
            status = "The Pi restarted. Waiting for a trip."
            say("The Pi restarted, so this trip ended. Start again.")
            return
        }
        if piReachable {
            piReachable = false
            Haptic.warning()
            say("I lost the Pi. \(lastSaid)")
        }
    }

    private func message(for error: Error) -> String {
        if case APIError.http(_, let msg) = error { return "Couldn't start the trip. \(msg)" }
        return "I can't reach the Pi. Check the hotspot."
    }
}
