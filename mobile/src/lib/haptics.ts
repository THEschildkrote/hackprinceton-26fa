// two things live here:
// 1. the band's haptic grammar (ported from web/companion.html / firmware/nudge_band/haptics.h), pure and clocked
//    by the caller, so the simulated band and tests run without hardware.
// 2. turning the server's vibrate patterns (ms on/off, from nudge/instruct.py CUES) into phone haptics.
import type { Act, Dir, LivePacket } from "./device";

export const L = 1, R = 2, BOTH = 3;
type Seg = [on: number, off: number, motors: number];

export const BAND_CUES: Record<string, Seg[]> = {
  left: [[120, 80, L], [120, 0, L]],
  right: [[120, 80, R], [120, 80, R], [120, 0, R]],
  bear_left: [[60, 60, L], [120, 0, L]],
  bear_right: [[60, 60, R], [120, 60, R], [120, 0, R]],
  uturn: [[400, 100, BOTH], [400, 0, BOTH]],
  floor: [[200, 100, BOTH], [200, 100, BOTH], [200, 0, BOTH]],
  arrive: [[60, 60, BOTH], [60, 60, BOTH], [300, 0, BOTH]],
  hazard: Array.from({ length: 5 }, (): Seg => [30, 30, BOTH]),
  off_route: [[500, 0, BOTH]],
  straight: [[60, 0, BOTH]],
};

export function approachCue(act: Act | null, dir: Dir): string | null {
  if (act === "turn") return dir === "L" ? "left" : dir === "R" ? "right" : null;
  if (act === "bear") return dir === "L" ? "bear_left" : dir === "R" ? "bear_right" : null;
  if (act === "uturn") return "uturn";
  if (act === "floor") return "floor";
  return null;
}

// steering pulse period: silent within 15 deg, 1.2 s at 15 deg down to 0.3 s at 90+ deg
export function steerPeriod(deg: number): number {
  const a = Math.abs(deg);
  if (a <= 15) return 0;
  if (a >= 90) return 300;
  return 1200 - (a - 15) * Math.floor(900 / 75);
}

export class BandEngine {
  private pat: Seg[] | null = null;
  private idx = 0;
  private on = true;
  private t0 = 0;
  private steer0 = 0;
  private live: LivePacket | null = null;
  private was = { off: false, done: false, hazard: false };
  private tapped = false;
  private last: { act: Act | null; dir: Dir } = { act: null, dir: "S" };
  played: string[] = []; // for tests and the log

  play(cue: string, now: number) {
    const p = BAND_CUES[cue];
    if (!p) return;
    this.pat = p; this.idx = 0; this.on = true; this.t0 = now;
    this.played.push(cue);
  }

  onLive(l: LivePacket, now: number) {
    if (l.off && !this.was.off) this.play("off_route", now);
    if (l.done && !this.was.done) this.play("arrive", now);
    if (l.hazard && !this.was.hazard) this.play("hazard", now);
    this.was = { off: l.off, done: l.done, hazard: !!l.hazard };
    if (l.act_next !== this.last.act || l.dir_next !== this.last.dir || (l.m_next != null && l.m_next > 3)) this.tapped = false;
    this.last = { act: l.act_next, dir: l.dir_next };
    if (!this.tapped && !l.done && l.m_next != null && l.m_next < 2) {
      const c = approachCue(l.act_next, l.dir_next);
      if (c) { this.play(c, now); this.tapped = true; }
    }
    this.live = l;
  }

  // which motors are on at time `now` (ms): bit L, bit R
  tick(now: number): number {
    if (this.pat) {
      for (;;) {
        const [on, off, m] = this.pat[this.idx];
        const dur = this.on ? on : off;
        if (now - this.t0 < dur) return this.on ? m : 0;
        this.t0 += dur;
        if (this.on) { this.on = false; continue; }
        if (++this.idx >= this.pat.length) { this.pat = null; this.steer0 = now; break; }
        this.on = true;
      }
    }
    const l = this.live;
    if (!l || l.done || l.off || l.deg_next == null) return 0;
    const p = steerPeriod(l.deg_next);
    if (!p) return 0;
    if (now - this.steer0 >= p) this.steer0 += p * Math.floor((now - this.steer0) / p);
    return now - this.steer0 < 40 ? (l.deg_next > 0 ? L : R) : 0;
  }
}

// --- phone haptics from a server pattern like [120, 80, 120] (on, off, on, ...)

// react native's Vibration on android takes [wait, on, off, on, ...]
export function androidPattern(ms: number[] | null | undefined): number[] {
  if (!ms || !ms.length) return [];
  return [0, ...ms];
}

export type Strength = "light" | "medium" | "heavy";
export interface Pulse { at: number; ms: number; strength: Strength }

// ios can't vibrate for arbitrary lengths; play one impact per "on" segment at the right moment instead
export function pulseSchedule(ms: number[] | null | undefined): Pulse[] {
  const out: Pulse[] = [];
  let t = 0;
  (ms ?? []).forEach((d, i) => {
    if (i % 2 === 0) out.push({ at: t, ms: d, strength: d <= 60 ? "light" : d <= 200 ? "medium" : "heavy" });
    t += d;
  });
  return out;
}
