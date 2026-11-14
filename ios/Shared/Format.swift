import Foundation

// spoken/visible metres: half-metre steps up close, whole metres beyond 10 (matches nudge/instruct.meters)
func fmtMetres(_ m: Double?) -> String {
    guard let m, !m.isNaN else { return "-" }
    if m < 0.75 { return "< 1 m" }
    let v = m < 10 ? (m * 2).rounded() / 2 : m.rounded()
    return v == v.rounded() ? "\(Int(v)) m" : String(format: "%.1f m", v)
}

// destination names from the place catalog: useful rooms first, corridors and closets last, no "Other".
// port of placeNames in mobile/src/lib/api.ts
enum PlaceOrder {
    static let first = ["bath", "restroom", "toilet", "kitchen", "living", "dining", "entr", "lobby", "reception",
                        "office", "conference", "meeting", "seminar", "atrium", "stair", "bedroom", "laundry", "elevator"]
    static let last = ["hall", "corridor", "closet", "storage"]
    static let hide = ["other", "void", "unknown"]

    static func rank(_ name: String) -> Int {
        let l = name.split(separator: ",").first.map(String.init)?.lowercased() ?? name.lowercased()
        if let i = first.firstIndex(where: { l.contains($0) }) { return i }
        return last.contains(where: { l.contains($0) }) ? 100 : 50
    }

    static func names(_ places: [Place]) -> [String] {
        let usable = places.filter { p in
            (p.kind == "room" || p.kind == "zone" || p.kind == "feature" || p.kind == "tenant")
                && !(p.unnamed ?? false)
                && !hide.contains(p.name.split(separator: ",").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? "")
        }
        let sorted = usable.sorted { a, b in
            let ka = a.kind == "feature" ? 1 : 0, kb = b.kind == "feature" ? 1 : 0
            if ka != kb { return ka < kb }
            let ra = rank(a.name), rb = rank(b.name)
            if ra != rb { return ra < rb }
            return a.name < b.name
        }
        var seen = Set<String>()
        return sorted.map(\.name).filter { seen.insert($0).inserted }
    }
}
