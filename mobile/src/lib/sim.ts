// a fake walker that follows the route polyline at walking speed, for demos without a localizer.
// positions are model-frame metres; heading is radians ccw from +x (same as nudge.geom.heading).

export type Point = [x: number, y: number, floor: string];
export interface Pose { x: number; y: number; floor: string; heading: number; done: boolean }

export class SimWalk {
  private seg = 0; // index of the segment start
  private t = 0;   // metres walked along the current segment
  private heading = 0;
  constructor(private pts: Point[], public speed = 1.0) {
    if (pts.length > 1) this.heading = Math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]);
  }

  pose(): Pose {
    const p = this.pts;
    if (!p.length) return { x: 0, y: 0, floor: "", heading: 0, done: true };
    if (this.seg >= p.length - 1) {
      const e = p[p.length - 1];
      return { x: e[0], y: e[1], floor: e[2], heading: this.heading, done: true };
    }
    const [a, b] = [p[this.seg], p[this.seg + 1]];
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const f = L === 0 ? 1 : Math.min(1, this.t / L);
    return { x: a[0] + f * (b[0] - a[0]), y: a[1] + f * (b[1] - a[1]), floor: a[2], heading: this.heading, done: false };
  }

  // advance dt seconds; floor changes (elevator/stairs links) are crossed in one step
  step(dt: number): Pose {
    let left = this.speed * dt;
    const p = this.pts;
    while (left > 0 && this.seg < p.length - 1) {
      const [a, b] = [p[this.seg], p[this.seg + 1]];
      if (a[2] !== b[2]) { this.seg += 1; this.t = 0; break; }
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (L > 0) this.heading = Math.atan2(b[1] - a[1], b[0] - a[0]);
      const rem = L - this.t;
      if (left < rem) { this.t += left; left = 0; }
      else { left -= rem; this.seg += 1; this.t = 0; }
    }
    return this.pose();
  }

  // jump to the next vertex: "i'm at the next turn"
  snapForward(): Pose {
    if (this.seg < this.pts.length - 1) { this.seg += 1; this.t = 0; }
    return this.pose();
  }

  // the server rerouted: keep walking from where we are along the new line
  reroute(pts: Point[]) {
    const here = this.pose();
    this.pts = [[here.x, here.y, here.floor], ...pts.slice(1)];
    this.seg = 0; this.t = 0;
  }
}

export function polylineLength(pts: Point[]): number {
  let s = 0;
  for (let i = 1; i < pts.length; i++) if (pts[i][2] === pts[i - 1][2]) s += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return s;
}
