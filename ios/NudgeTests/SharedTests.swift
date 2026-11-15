import XCTest

// ported from mobile/src/lib/__tests__/steps.test.ts, plus the back-mount heading math.
// these test the shared files directly (they're compiled into this bundle), no app host needed.

final class StepDetectorTests: XCTestCase {
    func testWalkWithNoiseCountsItsSteps() {
        XCTAssertLessThanOrEqual(abs(countSteps(walkingSamples(seconds: 20, hz: 1.8)) - 36), 1)
    }

    func testSlowAndBriskWalksBothCount() {
        XCTAssertLessThanOrEqual(abs(countSteps(walkingSamples(seconds: 20, hz: 1.3, amp: 0.15)) - 26), 1)
        XCTAssertLessThanOrEqual(abs(countSteps(walkingSamples(seconds: 20, hz: 2.3, amp: 0.4)) - 46), 1)
    }

    func testStandingStillCountsNothing() {
        XCTAssertEqual(countSteps(standingSamples(seconds: 30)), 0)
    }

    func testStandWalkStandCountsOnlyTheWalk() {
        let a = standingSamples(seconds: 5, seed: 1)
        let w = walkingSamples(seconds: 10, hz: 1.8, seed: 2).map { AccelSample(t: $0.t + 5000, x: $0.x, y: $0.y, z: $0.z) }
        let b = standingSamples(seconds: 5, seed: 3).map { AccelSample(t: $0.t + 15000, x: $0.x, y: $0.y, z: $0.z) }
        XCTAssertLessThanOrEqual(abs(countSteps(a + w + b) - 18), 1)
    }

    func testNeverTwoStepsWithin300ms() {
        var d = StepDetector()
        var times: [Double] = []
        for s in walkingSamples(seconds: 10, hz: 1.8) where d.push(s) { times.append(s.t) }
        for i in 1..<times.count { XCTAssertGreaterThanOrEqual(times[i] - times[i - 1], 300) }
    }

    func testWorksWhicheverWayThePhoneIsHeld() {
        let sideways = walkingSamples(seconds: 20, hz: 1.8).map { AccelSample(t: $0.t, x: $0.z, y: $0.y, z: $0.x) }
        XCTAssertLessThanOrEqual(abs(countSteps(sideways) - 36), 1)
    }
}

final class HeadingTests: XCTestCase {
    func testTrueHeadingWithMagneticFallback() {
        XCTAssertEqual(Heading.pick(trueHeading: 90, magneticHeading: 80), 90)
        XCTAssertEqual(Heading.pick(trueHeading: -1, magneticHeading: 80), 80)
    }

    func testSmoothingWrapsAroundNorth() {
        var f = HeadingFilter(alpha: 0.5)
        _ = f.push(350)
        let v = f.push(10)
        XCTAssertLessThan(min(v, 360 - v), 10)
    }

    func testAngleDifferencesAndFacingHints() {
        XCTAssertEqual(Heading.angleDiff(350, 10), 20)
        XCTAssertEqual(Heading.angleDiff(10, 350), -20)
        XCTAssertEqual(Heading.facingHint(current: 0, target: 10), "You're facing the right way.")
        XCTAssertEqual(Heading.facingHint(current: 0, target: 45), "Turn a little right.")
        XCTAssertEqual(Heading.facingHint(current: 0, target: 270), "Turn left.")
        XCTAssertEqual(Heading.facingHint(current: 0, target: 180), "Turn around.")
    }

    // reference frame: x north, y west, z up. the third row of coremotion's matrix is the device z axis
    // expressed in that frame, so (m31, m32, m33) is where the screen points.
    func testBackMountHeading() {
        // screen against the back, screen normal pointing north: walking north
        var h = Heading.walking(m31: 1, m32: 0, m33: 0, mount: .screenFacesBody)
        XCTAssertEqual(h.deg, 0, accuracy: 0.001)
        XCTAssertEqual(h.horizontal, 1, accuracy: 0.001)
        // screen normal pointing west (y = +1): walking west = 270
        h = Heading.walking(m31: 0, m32: 1, m33: 0, mount: .screenFacesBody)
        XCTAssertEqual(h.deg, 270, accuracy: 0.001)
        // screen facing out flips it: normal points west, so they walk east
        h = Heading.walking(m31: 0, m32: 1, m33: 0, mount: .screenFacesOut)
        XCTAssertEqual(h.deg, 90, accuracy: 0.001)
        // lying flat: no horizontal component
        h = Heading.walking(m31: 0, m32: 0, m33: 1, mount: .screenFacesBody)
        XCTAssertEqual(h.horizontal, 0, accuracy: 0.001)
    }
}

final class StepQueueTests: XCTestCase {
    func testFailedPostsNeverLoseSteps() {
        var q = StepQueue()
        q.add(); q.add()
        XCTAssertEqual(q.take(), 2)
        q.add()
        q.fail()
        XCTAssertEqual(q.take(), 3)
        q.ack()
        XCTAssertEqual(q.take(), 0)
        XCTAssertEqual(q.total, 3)
    }

    func testStepLengthHelperRejectsTypos() {
        XCTAssertEqual(stepLength(metres: 7), 0.7)
        XCTAssertNil(stepLength(metres: 70))
        XCTAssertNil(stepLength(metres: 0))
    }

    func testMetresFormatting() {
        XCTAssertEqual(fmtMetres(0.5), "< 1 m")
        XCTAssertEqual(fmtMetres(4.3), "4.5 m")
        XCTAssertEqual(fmtMetres(12.4), "12 m")
        XCTAssertEqual(fmtMetres(nil), "-")
    }
}

// a voice turn that starts a trip names it, and the traveller app walks it (Traveller.adoptVoiceTrip).
// the reply below is the server's own, captured from "take me to the finish in demo corner".
final class VoiceReplyTests: XCTestCase {
    func testAVoiceTurnThatStartsATripNamesItsBuildingAndGoal() throws {
        let json = #"{"say":"Okay, going to the Finish. Route ready: 12 meters, 1 turn. Walk 6 meters, then turn right. Say brief me any time to hear what to expect in this building.","session":"b6c6fcc5f7","pending":null,"choice":null,"state":{"remaining":12.0,"next":{"type":"turn","text":"Walk 6 meters, then turn right.","along":6.0,"cue":"right"},"room":"Finish","unmapped":false,"done":false,"reroutes":0,"distance_to_next_m":6.0,"bearing_to_next_deg":null,"path":[[1.0,1.0,"f1"],[1.0,3.5,"f1"],[1.0,7.0,"f1"],[3.5,7.0,"f1"],[7.0,7.0,"f1"]]},"device":{"packet":{"m_next":6.0,"deg_next":null,"act_next":"turn","dir_next":"R","off":false,"done":false,"unmapped":false,"hazard":false},"line":"N,6.0,,TR,0"},"trip":{"goal":"Finish","sid":"demo-corner","start":[1.0,1.0,"f1"]}}"#
        let r = try JSONDecoder().decode(VoiceReply.self, from: Data(json.utf8))
        XCTAssertEqual(r.session, "b6c6fcc5f7")
        XCTAssertEqual(r.trip?.sid, "demo-corner")
        XCTAssertEqual(r.trip?.goal, "Finish")
        XCTAssertNotNil(r.state?.remaining)
    }

    func testAnOrdinaryAnswerHasNoTrip() throws {
        let json = #"{"say":"About 6 meters to go.","session":null,"pending":null,"choice":null}"#
        let r = try JSONDecoder().decode(VoiceReply.self, from: Data(json.utf8))
        XCTAssertNil(r.trip)
        XCTAssertNil(r.session)
    }
}
