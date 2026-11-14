import SwiftUI

// the traveller's current floor, drawn from the walk graph: rooms, walls, the route, and their position.
// model coordinates are metres with y up; the view flips y so north-ish stays up like the floor plan.
struct FloorPlanView: View {
    let plan: FloorPlan
    let floor: String
    let route: [RoutePoint]
    let position: Position?

    private var walls: [Wall] { plan.floors.first { $0.id == floor }?.walls ?? [] }
    private var rooms: [Room] { plan.rooms.values.filter { $0.floor == floor && ($0.poly?.count ?? 0) >= 3 } }
    private var routeHere: [RoutePoint] { route.filter { $0.floor == floor } }

    // bounding box of everything on this floor, in metres
    private var bounds: CGRect {
        var xs: [Double] = [], ys: [Double] = []
        for w in walls { xs += [w.ax, w.bx]; ys += [w.ay, w.by] }
        for r in rooms { for p in r.poly ?? [] where p.count >= 2 { xs.append(p[0]); ys.append(p[1]) } }
        if let p = position { xs.append(p.x); ys.append(p.y) }
        guard let x0 = xs.min(), let x1 = xs.max(), let y0 = ys.min(), let y1 = ys.max() else {
            return CGRect(x: -10, y: -10, width: 20, height: 20)
        }
        return CGRect(x: x0, y: y0, width: max(1, x1 - x0), height: max(1, y1 - y0)).insetBy(dx: -1.5, dy: -1.5)
    }

    var body: some View {
        GeometryReader { geo in
            let b = bounds
            let scale = min(geo.size.width / b.width, geo.size.height / b.height)
            let ox = (geo.size.width - b.width * scale) / 2
            let oy = (geo.size.height - b.height * scale) / 2
            let map = { (x: Double, y: Double) -> CGPoint in
                CGPoint(x: ox + (x - b.minX) * scale, y: oy + (b.maxY - y) * scale)
            }
            ZStack {
                Canvas { ctx, _ in
                    // rooms, softly filled, with labels
                    for r in rooms {
                        guard let poly = r.poly else { continue }
                        var path = Path()
                        for (i, p) in poly.enumerated() where p.count >= 2 {
                            let pt = map(p[0], p[1])
                            if i == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
                        }
                        path.closeSubpath()
                        ctx.fill(path, with: .color(Theme.blue.opacity(0.07)))
                        if let label = r.label, !["Void", "Other", "Unknown"].contains(label) {
                            let cx = poly.compactMap { $0.first }.reduce(0, +) / Double(poly.count)
                            let cy = poly.compactMap { $0.count > 1 ? $0[1] : nil }.reduce(0, +) / Double(poly.count)
                            ctx.draw(Text(label).font(.caption2).foregroundStyle(.secondary), at: map(cx, cy))
                        }
                    }
                    // walls dark, doorways and openings light
                    for w in walls {
                        var p = Path()
                        p.move(to: map(w.ax, w.ay))
                        p.addLine(to: map(w.bx, w.by))
                        let open = w.kind != "wall"
                        ctx.stroke(p, with: .color(open ? .secondary.opacity(0.3) : .primary.opacity(0.75)),
                                   style: StrokeStyle(lineWidth: open ? 1 : 2, lineCap: .round, dash: open ? [3, 3] : []))
                    }
                    // the route still to walk
                    if routeHere.count >= 2 {
                        var p = Path()
                        p.move(to: map(routeHere[0].x, routeHere[0].y))
                        for pt in routeHere.dropFirst() { p.addLine(to: map(pt.x, pt.y)) }
                        ctx.stroke(p, with: .color(Theme.blue), style: StrokeStyle(lineWidth: 4, lineCap: .round, lineJoin: .round))
                        if let last = routeHere.last {
                            // the destination: a yellow sun with a navy ring
                            let e = map(last.x, last.y)
                            let dot = Path(ellipseIn: CGRect(x: e.x - 8, y: e.y - 8, width: 16, height: 16))
                            ctx.fill(dot, with: .color(Theme.yellow))
                            ctx.stroke(dot, with: .color(Theme.navy), lineWidth: 2.5)
                        }
                    }
                }
                if let p = position, p.floor == floor {
                    PositionDot().position(map(p.x, p.y))
                }
            }
        }
        .accessibilityElement()
        .accessibilityLabel(position == nil ? "Floor plan" : "Floor plan with the traveller's position")
    }
}

// red against the blue route, so the traveller is the first thing the eye finds
struct PositionDot: View {
    var body: some View {
        Circle().fill(Theme.red).frame(width: 16, height: 16)
            .overlay(Circle().stroke(.white, lineWidth: 3))
    }
}
