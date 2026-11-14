import CoreLocation
import CoreMotion
import Foundation

// the phone rides on the traveller's back, upright. a compass heading from core location uses the top edge,
// which points at the sky there, so it's useless. instead: device motion in a north-referenced frame, and the
// screen axis (forward or backward depending on the mount) projected onto the floor.
// the same 50 hz stream feeds the step detector (gravity + user acceleration = what an accelerometer reads).
final class Motion: NSObject, CLLocationManagerDelegate {
    private let motion = CMMotionManager()
    private let queue = OperationQueue()
    private let location = CLLocationManager()   // only so ios can give true north
    private var detector = StepDetector()
    private var filter = HeadingFilter(alpha: 0.2)

    var mount: Mount = .screenFacesBody
    var onStep: (() -> Void)?
    var onHeading: ((Double, Bool) -> Void)?   // smoothed walking heading, poor?
    private(set) var frameName = ""

    override init() {
        super.init()
        location.delegate = self
        queue.maxConcurrentOperationCount = 1
    }

    var available: Bool { motion.isDeviceMotionAvailable }

    func start() {
        guard motion.isDeviceMotionAvailable, !motion.isDeviceMotionActive else { return }
        if location.authorizationStatus == .notDetermined { location.requestWhenInUseAuthorization() }
        location.startUpdatingLocation()
        detector = StepDetector()
        filter = HeadingFilter(alpha: 0.2)
        let frames = CMMotionManager.availableAttitudeReferenceFrames()
        let frame: CMAttitudeReferenceFrame = frames.contains(.xTrueNorthZVertical) ? .xTrueNorthZVertical : .xMagneticNorthZVertical
        frameName = frame == .xTrueNorthZVertical ? "true north" : "magnetic north"
        motion.deviceMotionUpdateInterval = 1.0 / 50.0
        motion.showsDeviceMovementDisplay = true   // lets ios ask for a figure-eight when the compass is off
        motion.startDeviceMotionUpdates(using: frame, to: queue) { [weak self] dm, _ in
            guard let self, let dm else { return }
            let g = dm.gravity, a = dm.userAcceleration
            let sample = AccelSample(t: dm.timestamp * 1000, x: g.x + a.x, y: g.y + a.y, z: g.z + a.z)
            let stepped = self.detector.push(sample)
            let r = dm.attitude.rotationMatrix
            let h = Heading.walking(m31: r.m31, m32: r.m32, m33: r.m33, mount: self.mount)
            // lying flat (screen up or down) has no forward direction; keep the last heading
            var heading: Double? = nil
            if h.horizontal > 0.5 { heading = self.filter.push(h.deg) }
            let poor = h.horizontal <= 0.5 || dm.magneticField.accuracy == .uncalibrated
                || dm.magneticField.accuracy == .low
            let onStep = self.onStep, onHeading = self.onHeading
            let smooth = heading ?? self.filter.value
            DispatchQueue.main.async {
                if stepped { onStep?() }
                if let smooth { onHeading?(smooth, poor) }
            }
        }
    }

    func stop() {
        motion.stopDeviceMotionUpdates()
        location.stopUpdatingLocation()
    }
}
