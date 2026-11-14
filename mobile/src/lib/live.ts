// step bookkeeping for live mode: steps detected on the phone must all reach the pi, even across failed posts.
// pending = detected, not yet sent; inflight = in the request now. a failed request puts them back.

export class StepQueue {
  pending = 0;
  inflight = 0;
  total = 0;  // detected since start, for the readout

  add(n = 1) { this.pending += n; this.total += n; }

  // move everything pending into one request; returns how many steps to send (0 = heading-only)
  take(): number {
    this.inflight = this.pending;
    this.pending = 0;
    return this.inflight;
  }

  ack() { this.inflight = 0; }

  // the request didn't land: resend those steps with the next one
  fail() { this.pending += this.inflight; this.inflight = 0; }

  get busy() { return this.inflight > 0; }
}

// step length from the helper: "walk 10 steps, how many metres was that?"
export function stepLength(metres: number, steps = 10): number | null {
  if (!Number.isFinite(metres) || metres <= 0 || steps <= 0) return null;
  const m = metres / steps;
  return m >= 0.3 && m <= 1.2 ? Math.round(m * 100) / 100 : null;  // outside that range it's a typo
}
