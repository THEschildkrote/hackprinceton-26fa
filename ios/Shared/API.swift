import Foundation

// typed client for nudge/server.py and nudge/voice.py. all distances are metres.

struct NavEvent: Decodable {
    let type: String
    let text: String?
    let cue: String?
}

struct NextStep: Decodable {
    let type: String?
    let text: String?
}

struct NavState: Decodable {
    let remaining: Double?
    let next: NextStep?
    let done: Bool?
    let distance_to_next_m: Double?
    let bearing_to_next_deg: Double?
}

struct StartReply: Decodable {
    let session: String
    let events: [NavEvent]
    let state: NavState?
    let start_heading_deg: Double?
}

struct CalibrateReply: Decodable {
    let ok: Bool?
}

struct StepReply: Decodable {
    let say: [String]?
    let events: [NavEvent]?
    let state: NavState?
    let distance_to_next_m: Double?
    let bearing_to_next_deg: Double?
    let done: Bool?
    let waiting_for: String?
}

struct Building: Decodable, Identifiable, Hashable {
    let sid: String
    let address: String
    let city: String?
    let name: String?
    var id: String { sid }
    var label: String {
        if let city, !city.isEmpty { return "\(address), \(city)" }
        return address
    }
}

struct BuildingsReply: Decodable {
    let results: [Building]
    let demo: String?
}

struct Place: Decodable {
    let id: String?
    let name: String
    let kind: String
    let unnamed: Bool?
}

struct PlacesReply: Decodable {
    let places: [Place]
}

struct ResolvedPlace: Decodable {
    let name: String
}

struct Candidate: Decodable {
    let name: String
}

struct ResolveReply: Decodable {
    let best: ResolvedPlace?
    let ambiguous: Bool?
    let question: String?
    let candidates: [Candidate]?
    let message: String?
}

struct VoiceReply: Decodable {
    let say: String?
    let say_now: String?
    let pending_job: String?
    let done: Bool?
    let session: String?
    let pending: PendingAction?
    let state: NavState?
}

struct PendingAction: Decodable {
    let name: String?
}

struct RegisterReply: Decodable {
    let traveller_id: String?
    let code: String?
}

// remote trips: the caretaker sends one, the traveller's phone picks it up
struct Trip: Codable, Equatable {
    let sid: String
    let to: String
    let profile: String
    let ts: Double?
}

struct TripReply: Decodable {
    let trip: Trip?
}

struct PairReply: Decodable {
    let traveller_id: String
    let name: String?
}

struct Position: Decodable, Equatable {
    let x: Double
    let y: Double
    let floor: String
    let sid: String?
}

struct RecentEvent: Decodable, Identifiable, Equatable {
    let type: String?
    let text: String?
    let place: String?
    let floor: String?
    let ts: Double?
    var id: String { "\(ts ?? 0)-\(type ?? "")-\(text ?? "")" }
}

struct RoutePoints: Decodable, Equatable {
    let points: [RoutePoint]
}

// [x, y, floor] from the server
struct RoutePoint: Decodable, Equatable {
    let x: Double
    let y: Double
    let floor: String
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        x = try c.decode(Double.self)
        y = try c.decode(Double.self)
        floor = (try? c.decode(String.self)) ?? ""
    }
}

struct TravellerStatus: Decodable, Equatable {
    let name: String?
    let state: String?           // idle, walking, arrived, off_route, unmapped, help, rerouting
    let place: String?
    let floor: String?
    let goal: String?
    let remaining_m: Double?
    let eta_min: Double?
    let last_update_ts: Double?
    let recent: [RecentEvent]?
    let session: String?
    let sid: String?
    let position: Position?
    let route: RoutePoints?
}

// a caretaker message from the live feed (same wording and rate limits as the imessage texts)
struct CareMessage: Decodable, Identifiable, Equatable {
    let text: String
    let urgent: Bool?
    let ts: Double?
    var id: String { "\(ts ?? 0)-\(text)" }
}

// floor plan for the caretaker's live map (GET /api/graph/{sid})
struct Wall: Decodable, Equatable {
    let ax: Double, ay: Double, bx: Double, by: Double
    let kind: String
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()   // [[ax, ay], [bx, by], type]
        let a = try c.decode([Double].self)
        let b = try c.decode([Double].self)
        kind = (try? c.decode(String.self)) ?? "wall"
        ax = a[0]; ay = a[1]; bx = b[0]; by = b[1]
    }
}

struct Floor: Decodable, Identifiable, Equatable {
    let id: String
    let label: String
    let walls: [Wall]
}

struct Room: Decodable, Equatable {
    let label: String?
    let floor: String?
    let poly: [[Double]]?
}

struct FloorPlan: Decodable, Equatable {
    let floors: [Floor]
    let rooms: [String: Room]
}

enum APIError: LocalizedError {
    case unreachable(String)
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .unreachable(let base): return "Can't reach the Pi at \(base)."
        case .http(let code, let msg): return "Pi said \(code): \(msg)"
        }
    }

    var isUnknownSession: Bool {
        if case .http(404, let msg) = self { return msg.lowercased().contains("unknown session") }
        return false
    }
}

struct API {
    let base: String

    private func url(_ path: String, query: [URLQueryItem] = []) -> URL? {
        let trimmed = base.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        guard var c = URLComponents(string: trimmed + path) else { return nil }
        if !query.isEmpty { c.queryItems = query }
        return c.url
    }

    private func send<T: Decodable>(_ method: String, _ path: String, query: [URLQueryItem] = [],
                                    body: [String: Any]? = nil, timeout: TimeInterval = 10) async throws -> T {
        guard let u = url(path, query: query) else { throw APIError.unreachable(base) }
        var req = URLRequest(url: u, timeoutInterval: timeout)
        req.httpMethod = method
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let result: (Data, URLResponse)
        do {
            result = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.unreachable(base)
        }
        let (data, resp) = result
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            var msg = "\(code)"
            if let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let d = j["detail"] as? String { msg = d }
                else if let d = j["detail"] as? [String: Any], let m = d["message"] as? String ?? d["question"] as? String { msg = m }
            }
            throw APIError.http(code, msg)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    // a cheap call that proves the pi is there
    func ping() async throws {
        let _: [String: AnyDecodable] = try await send("GET", "/api/profiles", timeout: 5)
    }

    func buildings(_ q: String) async throws -> BuildingsReply {
        try await send("GET", "/api/buildings", query: [.init(name: "q", value: q), .init(name: "limit", value: "12")])
    }

    func places(_ sid: String) async throws -> PlacesReply {
        try await send("GET", "/api/places/\(sid)", timeout: 30)
    }

    func resolve(_ sid: String, _ q: String, profile: String) async throws -> ResolveReply {
        try await send("GET", "/api/places/\(sid)/resolve",
                       query: [.init(name: "q", value: q), .init(name: "profile", value: profile)], timeout: 30)
    }

    // the pi resolves "entrance" to the building's way in; resolving it can take a while on first use
    func start(sid: String, to: String, profile: String, traveller: String?) async throws -> StartReply {
        var body: [String: Any] = ["sid": sid, "to": to, "profile": profile, "start": "entrance"]
        if let traveller { body["traveller"] = traveller }
        return try await send("POST", "/api/nav/start", body: body, timeout: 30)
    }

    func calibrate(session: String, headingDeg: Double) async throws -> CalibrateReply {
        try await send("POST", "/api/nav/calibrate", body: ["session": session, "heading_deg": headingDeg], timeout: 5)
    }

    // new steps since the last call (0 = heading only); short timeout so a dropped packet doesn't stall the walk
    func step(session: String, steps: Int, headingDeg: Double?, stepM: Double) async throws -> StepReply {
        var body: [String: Any] = ["session": session, "steps": steps, "step_m": stepM]
        if let headingDeg { body["heading_deg"] = headingDeg }
        return try await send("POST", "/api/nav/step", body: body, timeout: 4)
    }

    // problems ("door is locked") go to the exception path; async_ok gets an instant reply plus a job to poll
    func voiceTurn(traveller: String, text: String, session: String?, sid: String?) async throws -> VoiceReply {
        var body: [String: Any] = ["traveller": traveller, "text": text, "async_ok": true]
        if let session { body["session"] = session }
        if let sid { body["sid"] = sid }
        return try await send("POST", "/api/voice/turn", body: body, timeout: 30)
    }

    func voiceJob(_ id: String) async throws -> VoiceReply {
        try await send("GET", "/api/voice/job/\(id)", timeout: 10)
    }

    func register(traveller: String, name: String?) async throws -> RegisterReply {
        var body: [String: Any] = ["traveller_id": traveller]
        if let name, !name.isEmpty { body["name"] = name }
        return try await send("POST", "/api/traveller/register", body: body, timeout: 10)
    }

    // traveller side: is there a trip waiting? then acknowledge it once started
    func pendingTrip(traveller: String) async throws -> Trip? {
        let r: TripReply = try await send("GET", "/api/traveller/\(traveller)/trip", timeout: 5)
        return r.trip
    }

    func ackTrip(traveller: String) async throws {
        let _: [String: AnyDecodable] = try await send("POST", "/api/traveller/\(traveller)/trip/ack", body: [:], timeout: 5)
    }

    // caretaker side
    func pair(code: String) async throws -> PairReply {
        try await send("POST", "/api/caretaker/pair", body: ["code": code], timeout: 10)
    }

    func status(traveller: String) async throws -> TravellerStatus {
        try await send("GET", "/api/traveller/\(traveller)/status", timeout: 6)
    }

    func sendTrip(traveller: String, sid: String, to: String, profile: String) async throws {
        let _: [String: AnyDecodable] = try await send("POST", "/api/traveller/\(traveller)/trip",
                                                     body: ["sid": sid, "to": to, "profile": profile], timeout: 10)
    }

    func floorPlan(sid: String) async throws -> FloorPlan {
        try await send("GET", "/api/graph/\(sid)", timeout: 30)
    }

    // server-sent events: one CareMessage per "data:" line, live, until the stream ends or the task is cancelled
    func messages(traveller: String, after: Int = 0) -> AsyncThrowingStream<CareMessage, Error> {
        AsyncThrowingStream { cont in
            let task = Task {
                guard let u = url("/api/traveller/\(traveller)/events", query: [.init(name: "after", value: "\(after)")]) else {
                    cont.finish(throwing: APIError.unreachable(base)); return
                }
                var req = URLRequest(url: u, timeoutInterval: 3600)
                req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                do {
                    let (bytes, _) = try await URLSession.shared.bytes(for: req)
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data:") else { continue }
                        let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                        if let d = json.data(using: .utf8), let m = try? JSONDecoder().decode(CareMessage.self, from: d) {
                            cont.yield(m)
                        }
                    }
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }
}

// decodes any json value; enough to check that /api/profiles answered with json
struct AnyDecodable: Decodable {
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { return }
        if (try? c.decode(Bool.self)) != nil { return }
        if (try? c.decode(Double.self)) != nil { return }
        if (try? c.decode(String.self)) != nil { return }
        if (try? c.decode([AnyDecodable].self)) != nil { return }
        if (try? c.decode([String: AnyDecodable].self)) != nil { return }
    }
}
