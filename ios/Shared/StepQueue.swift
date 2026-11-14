import Foundation

// every step detected on the phone must reach the pi, even across failed posts. port of mobile/src/lib/live.ts.
// pending = detected, not yet sent; inflight = in the request now. a failed request puts them back.
struct StepQueue {
    private(set) var pending = 0
    private(set) var inflight = 0
    private(set) var total = 0   // detected since start, for the readout

    mutating func add(_ n: Int = 1) {
        pending += n
        total += n
    }

    // move everything pending into one request; how many steps to send (0 = heading only)
    mutating func take() -> Int {
        inflight = pending
        pending = 0
        return inflight
    }

    mutating func ack() { inflight = 0 }

    // the request didn't land: resend those steps with the next one
    mutating func fail() {
        pending += inflight
        inflight = 0
    }

    var busy: Bool { inflight > 0 }
}

// step length from the helper: "walk 10 steps, how many metres was that?"
func stepLength(metres: Double, steps: Int = 10) -> Double? {
    guard metres.isFinite, metres > 0, steps > 0 else { return nil }
    let m = metres / Double(steps)
    guard m >= 0.3, m <= 1.2 else { return nil }   // outside that range it's a typo
    return (m * 100).rounded() / 100
}
