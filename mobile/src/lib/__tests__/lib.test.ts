import fixture from "./fixtures/device_lines.json";
import { asciiToB64, b64ToAscii, decodeLive, decodeStep, encodeLive, encodeStep, parseButton } from "../device";
import { BandEngine, L, R, androidPattern, pulseSchedule, steerPeriod } from "../haptics";
import { SimWalk, polylineLength, type Point } from "../sim";
import { calibrateOffset, modelHeading, StepTracker } from "../pdr";
import { describePending, entryNode, fmtMetres, type Graph } from "../api";

// the fixture holds real lines from nudge/device.py walking the synthetic building, with python's decoding

describe("device lines (python round-trip)", () => {
  test("route steps decode like python and re-encode byte for byte", () => {
    for (const { line, decoded } of fixture.route) {
      const d = decodeStep(line)!;
      expect(d).not.toBeNull();
      expect(d.act).toBe(decoded.act);
      expect(d.dir).toBe(decoded.dir);
      expect(d.deg).toBe(decoded.deg);
      expect(d.m).toBeCloseTo(decoded.m, 6);
      expect(encodeStep(d)).toBe(line);
      expect(line.length).toBeLessThan(20);
    }
  });

  test("live lines decode like python and re-encode byte for byte", () => {
    for (const { line, decoded } of fixture.live) {
      const d = decodeLive(line)!;
      expect(d.m_next).toBe(decoded.m_next);
      expect(d.deg_next).toBe(decoded.deg_next);
      expect(d.act_next).toBe(decoded.act_next ?? null);
      expect(d.off).toBe(decoded.off);
      expect(d.done).toBe(decoded.done);
      expect(encodeLive(d)).toBe(line);
    }
    expect(fixture.live.some((l) => l.decoded.done)).toBe(true);
  });

  test("garbage is rejected, buttons parse", () => {
    for (const bad of ["", "N,1", "X,1,2,TL,0", "3,Q,L,90,1.0", "2,T,Z,90,1.0", "N,1.0,5,T,0"]) {
      expect(decodeLive(bad) ?? decodeStep(bad)).toBeNull();
    }
    expect(parseButton("B,repeat")).toBe("repeat");
    expect(parseButton("B,lost\n")).toBe("lost");
    expect(parseButton("B,x")).toBeNull();
  });
});

describe("band grammar", () => {
  test("steering period matches the firmware table", () => {
    expect(steerPeriod(10)).toBe(0);
    expect(steerPeriod(15)).toBe(0);
    expect(steerPeriod(16)).toBe(1188);
    expect(steerPeriod(90)).toBe(300);
    expect(steerPeriod(-120)).toBe(300);
  });

  test("left turn ahead: two pulses on the left motor, once", () => {
    const e = new BandEngine();
    e.onLive({ m_next: 1.5, deg_next: 0, act_next: "turn", dir_next: "L", off: false, done: false }, 0);
    e.onLive({ m_next: 1.0, deg_next: 0, act_next: "turn", dir_next: "L", off: false, done: false }, 50);
    expect(e.played).toEqual(["left"]);
    const motors = [0, 150, 250, 350].map((t) => e.tick(t));
    expect(motors).toEqual([L, 0, L, 0]); // on 0-120, off 120-200, on 200-320, then done
  });

  test("right turn is three pulses on the right; drifting right pulses the right motor", () => {
    const e = new BandEngine();
    e.onLive({ m_next: 1.0, deg_next: 0, act_next: "turn", dir_next: "R", off: false, done: false }, 0);
    let pulses = 0, prev = 0;
    for (let t = 0; t < 600; t += 10) { const m = e.tick(t); if (m === R && prev !== R) pulses++; prev = m; }
    expect(pulses).toBe(3);
    const d = new BandEngine();
    d.onLive({ m_next: 6, deg_next: -60, act_next: "turn", dir_next: "R", off: false, done: false }, 0);
    expect(d.tick(0)).toBe(R);
    expect(d.tick(100)).toBe(0);
  });

  test("off route, arrive and hazard fire once on the edge", () => {
    const e = new BandEngine();
    const base = { m_next: 5, deg_next: 0, act_next: "walk" as const, dir_next: "S" as const, off: false, done: false };
    e.onLive({ ...base, off: true }, 0);
    e.onLive({ ...base, off: true }, 10);
    e.onLive({ ...base, hazard: true }, 600);
    e.onLive({ ...base, done: true }, 1200);
    expect(e.played).toEqual(["off_route", "hazard", "arrive"]);
  });

  test("phone patterns", () => {
    expect(androidPattern([120, 80, 120])).toEqual([0, 120, 80, 120]);
    expect(androidPattern(null)).toEqual([]);
    expect(pulseSchedule([60, 60, 60, 60, 300])).toEqual([
      { at: 0, ms: 60, strength: "light" }, { at: 120, ms: 60, strength: "light" }, { at: 240, ms: 300, strength: "heavy" },
    ]);
  });
});

describe("sim walk", () => {
  const pts = fixture.route_points as Point[];

  test("walks the whole polyline at speed and crosses the floor change", () => {
    const w = new SimWalk(pts, 1.2);
    const total = polylineLength(pts);
    let steps = 0, pose = w.pose();
    const floors = new Set([pose.floor]);
    while (!pose.done && steps < 1000) { pose = w.step(0.5); floors.add(pose.floor); steps++; }
    expect(pose.done).toBe(true);
    expect(floors).toEqual(new Set(["f1", "f2"]));
    // ~total/0.6 m per step, plus one step swallowed by the floor change
    expect(steps).toBeGreaterThanOrEqual(Math.floor(total / 0.6));
    expect(steps).toBeLessThanOrEqual(Math.ceil(total / 0.6) + 3);
    const end = pts[pts.length - 1];
    expect([pose.x, pose.y, pose.floor]).toEqual([end[0], end[1], end[2]]);
  });

  test("heading follows the segment, snapForward jumps a vertex", () => {
    const w = new SimWalk([[0, 0, "f"], [0, 5, "f"], [5, 5, "f"]], 1);
    expect(w.step(1).heading).toBeCloseTo(Math.PI / 2);
    expect(w.snapForward()).toMatchObject({ x: 0, y: 5 });
    expect(w.step(1).heading).toBeCloseTo(0);
  });
});

describe("pdr and helpers", () => {
  test("calibration makes the current compass reading equal the route heading", () => {
    const off = calibrateOffset(1.0, 20, -5);
    expect(modelHeading(off, 20, -5)).toBeCloseTo(1.0);
  });

  test("step tracker advances along the heading in metres", () => {
    const t = new StepTracker(0, 0, "f1", Math.PI / 2, 0.7);
    t.onSteps(100); // first reading is the baseline
    expect(t.onSteps(110)).toBe(10);
    expect(t.x).toBeCloseTo(0);
    expect(t.y).toBeCloseTo(7);
  });

  test("entry node prefers a lobby, metres format", () => {
    const g: Graph = { sid: "s", floors: [{ id: "f1", label: "Floor 1" }], warnings: [], nodes: [
      { id: "a", label: "1", x: 0, y: 0, z: 0, floor: "f1", room: "r1", room_label: "Kitchen", tags: [] },
      { id: "b", label: "2", x: 9, y: 9, z: 0, floor: "f1", room: "r2", room_label: "Lobby", tags: [] },
    ] };
    expect(entryNode(g)!.id).toBe("b");
    expect(fmtMetres(0.4)).toBe("< 1 m");
    expect(fmtMetres(4.3)).toBe("4.5 m");
    expect(fmtMetres(12.4)).toBe("12 m");
  });
});

test("base64 matches node and round-trips every fixture line", () => {
  for (const { line } of [...fixture.route, ...fixture.live]) {
    expect(asciiToB64(line)).toBe(Buffer.from(line, "ascii").toString("base64"));
    expect(b64ToAscii(asciiToB64(line))).toBe(line);
  }
  expect(b64ToAscii(Buffer.from("B,lost").toString("base64"))).toBe("B,lost");
});

test("pending reroute reads as plain words", () => {
  expect(describePending({ name: "commit_reroute", args: { block_node: "elevator", goal: "Conference Room", reason: "Elevator out" } }))
    .toBe("New route: avoid the elevator and go to the Conference Room. Reason: Elevator out.");
  expect(describePending({ name: "commit_reroute", args: { block_node: "p_E1" } })).toBe("New route: avoid the E1.");
});
