// real-time step detection from the accelerometer (units: g, time: ms). pure, no expo imports, so it's testable.
//
// magnitude |a| minus a slow gravity baseline, low-passed, then a peak with hysteresis: a step fires when the
// signal rises above an adaptive threshold after having dipped below zero, at most once per MIN_GAP_MS.
// the threshold follows the walking amplitude, so a gentle walk and a heavy one both count, while standing
// still (noise well under MIN_PEAK) counts nothing.

export interface Sample { t: number; x: number; y: number; z: number }

export const MIN_GAP_MS = 300;   // nobody steps faster than ~3.3 per second
export const MIN_PEAK = 0.045;   // g; hand tremor and table noise stay below this

export class StepDetector {
  private gravity = 1;      // slow baseline of |a|
  private lp = 0;           // low-passed, gravity-free signal
  private amp = 0.06;       // running mean of |lp|, sets the threshold
  private armed = false;    // dipped below zero since the last step
  private last = -Infinity; // time of the last step
  private started = false;
  count = 0;

  constructor(private minGap = MIN_GAP_MS, private minPeak = MIN_PEAK) {}

  // feed one sample; true when it completes a step
  push(s: Sample): boolean {
    const m = Math.sqrt(s.x * s.x + s.y * s.y + s.z * s.z);
    if (!this.started) { this.gravity = m; this.started = true; }
    this.gravity += 0.02 * (m - this.gravity);
    this.lp += 0.35 * (m - this.gravity - this.lp);
    this.amp += 0.02 * (Math.abs(this.lp) - this.amp);
    const high = Math.max(this.minPeak, 0.75 * this.amp);
    if (this.lp < 0) this.armed = true;
    if (this.armed && this.lp > high && s.t - this.last >= this.minGap) {
      this.armed = false;
      this.last = s.t;
      this.count++;
      return true;
    }
    return false;
  }

  reset() { this.armed = false; this.last = -Infinity; this.count = 0; }
}

export function countSteps(samples: Sample[]): number {
  const d = new StepDetector();
  for (const s of samples) d.push(s);
  return d.count;
}

// synthetic walk for tests and the dev-only fake walk: vertical bounce at `hz` steps per second plus noise
export function walkingSamples(seconds: number, hz = 1.8, opts: { rate?: number; amp?: number; noise?: number; seed?: number } = {}): Sample[] {
  const rate = opts.rate ?? 50, amp = opts.amp ?? 0.25, noise = opts.noise ?? 0.03;
  let seed = opts.seed ?? 7;
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648 - 0.5; };
  const out: Sample[] = [];
  for (let i = 0; i < seconds * rate; i++) {
    const t = (i * 1000) / rate, ph = 2 * Math.PI * hz * (t / 1000);
    // a walking bounce is not a pure sine: add a second harmonic, like heel strike then push-off
    const bounce = amp * Math.sin(ph) + 0.3 * amp * Math.sin(2 * ph + 0.6);
    out.push({ t, x: noise * rnd(), y: noise * rnd(), z: 1 + bounce + noise * rnd() });
  }
  return out;
}

export function standingSamples(seconds: number, opts: { rate?: number; noise?: number; seed?: number } = {}): Sample[] {
  return walkingSamples(seconds, 1, { rate: opts.rate, amp: 0, noise: opts.noise ?? 0.015, seed: opts.seed });
}
