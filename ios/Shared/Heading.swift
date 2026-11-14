import Foundation

// compass heading helpers. degrees, 0 = north, clockwise (what ios reports and the pi expects).
// port of mobile/src/lib/heading.ts.

enum Heading {
    // ios gives trueHeading -1 when it can't correct for declination; then use magnetic
    static func pick(trueHeading: Double, magneticHeading: Double) -> Double {
        norm(trueHeading >= 0 ? trueHeading : magneticHeading)
    }

    // corelocation headingAccuracy is the error in degrees; negative means invalid.
    // over 35° (the old "level 1" and worse) is worth an interference hint
    static func poorAccuracy(_ accuracyDeg: Double) -> Bool {
        accuracyDeg < 0 || accuracyDeg > 35
    }

    static func norm(_ d: Double) -> Double {
        let v = d.truncatingRemainder(dividingBy: 360)
        return v < 0 ? v + 360 : v
    }

    // signed smallest turn from a to b, in (-180, 180]; positive = clockwise (to the right)
    static func angleDiff(_ a: Double, _ b: Double) -> Double {
        var d = norm(b - a)
        if d > 180 { d -= 360 }
        return d
    }

    // a few words for where to face before calibrating
    static func facingHint(current: Double, target: Double) -> String {
        let d = angleDiff(current, target)
        let a = abs(d)
        if a <= 20 { return "You're facing the right way." }
        let side = d > 0 ? "right" : "left"
        if a >= 150 { return "Turn around." }
        return a < 60 ? "Turn a little \(side)." : "Turn \(side)."
    }
}

// how the phone rides on the traveller's back. the phone stands upright (top edge up) in a back pocket,
// a backpack pouch or a harness. the screen normal (+z in device coordinates) is the axis we use:
//   screenFacesBody: screen against the wearer's back, so +z points forward (where they walk)
//   screenFacesOut:  screen facing away from the body, so +z points backward and forward is -z
enum Mount: String, CaseIterable, Identifiable {
    case screenFacesBody, screenFacesOut
    var id: String { rawValue }
    var label: String { self == .screenFacesBody ? "Screen faces my back" : "Screen faces out" }
}

extension Heading {
    // walking heading from a coremotion attitude in an x-north / z-up reference frame
    // (.xTrueNorthZVertical or .xMagneticNorthZVertical; there y points west).
    // coremotion's rotation matrix r maps reference vectors into device coordinates, so a device vector v
    // in the reference frame is rᵀv. for v = (0, 0, s) that is s * (m31, m32, m33).
    // returns degrees clockwise from north, and how horizontal the forward axis is (0 = lying flat, 1 = upright).
    static func walking(m31: Double, m32: Double, m33: Double, mount: Mount) -> (deg: Double, horizontal: Double) {
        let s = mount == .screenFacesBody ? 1.0 : -1.0
        let north = s * m31
        let east = -s * m32   // reference y points west
        let horizontal = (north * north + east * east).squareRoot()
        return (norm(atan2(east, north) * 180 / .pi), min(1, horizontal))
    }
}

// circular smoothing: average unit vectors, so 359° and 1° average to 0°, not 180°
struct HeadingFilter {
    private var sx = 0.0
    private var sy = 0.0
    private var seen = false
    let alpha: Double

    init(alpha: Double = 0.3) { self.alpha = alpha }

    mutating func push(_ deg: Double) -> Double {
        let r = deg * .pi / 180
        if !seen {
            sx = sin(r); sy = cos(r); seen = true
        } else {
            sx += alpha * (sin(r) - sx)
            sy += alpha * (cos(r) - sy)
        }
        return value ?? deg
    }

    var value: Double? {
        guard seen else { return nil }
        return Heading.norm(atan2(sx, sy) * 180 / .pi)
    }
}
