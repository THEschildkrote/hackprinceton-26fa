// compass heading helpers. degrees, 0 = north, clockwise (what ios reports and the pi expects).

export interface RawHeading { trueHeading: number; magHeading: number; accuracy: number }

// ios gives trueHeading -1 when it can't correct for declination; then use magnetic
export function pickHeading(h: RawHeading): number {
  const v = h.trueHeading != null && h.trueHeading >= 0 ? h.trueHeading : h.magHeading;
  return norm(v);
}

// ios accuracy levels: 3 < 20°, 2 < 35°, 1 < 50°, 0 worse; below 2 is worth a hint
export const poorAccuracy = (accuracy: number) => accuracy < 2;

export function norm(d: number): number {
  const v = d % 360;
  return v < 0 ? v + 360 : v;
}

// signed smallest turn from a to b, in (-180, 180]; positive = clockwise (to the right)
export function angleDiff(a: number, b: number): number {
  let d = norm(b - a);
  if (d > 180) d -= 360;
  return d;
}

// circular smoothing: average unit vectors, so 359° and 1° average to 0°, not 180°
export class HeadingFilter {
  private sx = 0;
  private sy = 0;
  private seen = false;
  constructor(private alpha = 0.3) {}

  push(deg: number): number {
    const r = (deg * Math.PI) / 180;
    if (!this.seen) { this.sx = Math.sin(r); this.sy = Math.cos(r); this.seen = true; }
    else {
      this.sx += this.alpha * (Math.sin(r) - this.sx);
      this.sy += this.alpha * (Math.cos(r) - this.sy);
    }
    return this.value() ?? deg;
  }

  value(): number | null {
    if (!this.seen) return null;
    return norm((Math.atan2(this.sx, this.sy) * 180) / Math.PI);
  }
}

// a few words for where to face: "turn a little left", for the facing hint before calibrating
export function facingHint(current: number, target: number): string {
  const d = angleDiff(current, target);
  const a = Math.abs(d);
  if (a <= 20) return "You're facing the right way.";
  const side = d > 0 ? "right" : "left";
  if (a >= 150) return "Turn around.";
  return a < 60 ? `Turn a little ${side}.` : `Turn ${side}.`;
}
