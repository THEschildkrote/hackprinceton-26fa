import Foundation

// real-time step detection from the accelerometer (units: g, time: ms). pure, no frameworks, so it's testable.
// port of mobile/src/lib/steps.ts: |a| minus a slow gravity baseline, low-passed, then a peak with hysteresis.
// a step fires when the signal rises above an adaptive threshold after having dipped below zero,
// at most once per minGap. the threshold follows the walking amplitude, so standing still counts nothing.

struct AccelSample {
    var t: Double   // ms
    var x: Double   // g
    var y: Double
    var z: Double
}

struct StepDetector {
    static let minGapMs = 300.0   // nobody steps faster than ~3.3 per second
    static let minPeak = 0.045    // g; hand tremor and table noise stay below this

    private var gravity = 1.0     // slow baseline of |a|
    private var lp = 0.0          // low-passed, gravity-free signal
    private var amp = 0.06        // running mean of |lp|, sets the threshold
    private var armed = false     // dipped below zero since the last step
    private var last = -Double.infinity
    private var started = false
    private(set) var count = 0

    let minGap: Double
    let minPeakG: Double

    init(minGap: Double = StepDetector.minGapMs, minPeak: Double = StepDetector.minPeak) {
        self.minGap = minGap
        self.minPeakG = minPeak
    }

    // feed one sample; true when it completes a step
    mutating func push(_ s: AccelSample) -> Bool {
        let m = (s.x * s.x + s.y * s.y + s.z * s.z).squareRoot()
        if !started { gravity = m; started = true }
        gravity += 0.02 * (m - gravity)
        lp += 0.35 * (m - gravity - lp)
        amp += 0.02 * (abs(lp) - amp)
        let high = max(minPeakG, 0.75 * amp)
        if lp < 0 { armed = true }
        if armed && lp > high && s.t - last >= minGap {
            armed = false
            last = s.t
            count += 1
            return true
        }
        return false
    }

    mutating func reset() {
        armed = false
        last = -.infinity
        count = 0
    }
}

func countSteps(_ samples: [AccelSample]) -> Int {
    var d = StepDetector()
    for s in samples { _ = d.push(s) }
    return d.count
}

// synthetic walk for tests: vertical bounce at `hz` steps per second plus noise.
// the generator does its arithmetic in doubles, exactly like the typescript version.
func walkingSamples(seconds: Double, hz: Double = 1.8, rate: Double = 50, amp: Double = 0.25,
                    noise: Double = 0.03, seed: Double = 7) -> [AccelSample] {
    var seed = seed
    func rnd() -> Double {
        seed = (seed * 1103515245 + 12345).truncatingRemainder(dividingBy: 2147483648)
        return seed / 2147483648 - 0.5
    }
    var out: [AccelSample] = []
    let n = Int(seconds * rate)
    out.reserveCapacity(n)
    for i in 0..<n {
        let t = Double(i) * 1000 / rate
        let ph = 2 * Double.pi * hz * (t / 1000)
        // a walking bounce is not a pure sine: add a second harmonic, like heel strike then push-off
        let bounce = amp * sin(ph) + 0.3 * amp * sin(2 * ph + 0.6)
        out.append(AccelSample(t: t, x: noise * rnd(), y: noise * rnd(), z: 1 + bounce + noise * rnd()))
    }
    return out
}

func standingSamples(seconds: Double, rate: Double = 50, noise: Double = 0.015, seed: Double = 7) -> [AccelSample] {
    walkingSamples(seconds: seconds, hz: 1, rate: rate, amp: 0, noise: noise, seed: seed)
}
