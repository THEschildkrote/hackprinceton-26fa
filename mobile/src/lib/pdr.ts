// experimental step-and-heading (pedestrian dead reckoning). it drifts: a few percent of distance, and phone
// compasses are poor near steel. good enough to feel the loop, not to trust.
// model frame: metres, heading radians ccw from +x.

export const STEP_M = 0.7;

// field angle in the device frame, phone held flat. turning the phone clockwise turns this counter-clockwise,
// so model heading = offset - fieldAngle; calibration solves for offset.
export const fieldAngle = (mx: number, my: number) => Math.atan2(my, mx);

export function calibrateOffset(routeHeading: number, mx: number, my: number): number {
  return wrap(routeHeading + fieldAngle(mx, my));
}

export function modelHeading(offset: number, mx: number, my: number): number {
  return wrap(offset - fieldAngle(mx, my));
}

export function wrap(a: number): number {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

export class StepTracker {
  private lastSteps: number | null = null;
  constructor(public x: number, public y: number, public floor: string, public heading = 0, public stepM = STEP_M) {}

  // pedometer reports a running count since the watch started
  onSteps(total: number) {
    const n = this.lastSteps == null ? 0 : Math.max(0, total - this.lastSteps);
    this.lastSteps = total;
    this.x += n * this.stepM * Math.cos(this.heading);
    this.y += n * this.stepM * Math.sin(this.heading);
    return n;
  }

  setHeading(h: number) { this.heading = wrap(h); }

  // the server knows better (e.g. after a snap or reroute)
  reset(x: number, y: number, floor: string) { this.x = x; this.y = y; this.floor = floor; }
}
