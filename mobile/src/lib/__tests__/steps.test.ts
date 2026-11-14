import { angleDiff, facingHint, HeadingFilter, pickHeading } from "../heading";
import { StepQueue, stepLength } from "../live";
import { countSteps, StepDetector, standingSamples, walkingSamples } from "../steps";

describe("step detector", () => {
  test("a 1.8 Hz walk with noise counts its steps", () => {
    const n = countSteps(walkingSamples(20, 1.8));
    expect(Math.abs(n - 36)).toBeLessThanOrEqual(1);
  });

  test("a slow walk and a brisk walk both count", () => {
    expect(Math.abs(countSteps(walkingSamples(20, 1.3, { amp: 0.15 })) - 26)).toBeLessThanOrEqual(1);
    expect(Math.abs(countSteps(walkingSamples(20, 2.3, { amp: 0.4 })) - 46)).toBeLessThanOrEqual(1);
  });

  test("standing still counts nothing", () => {
    expect(countSteps(standingSamples(30))).toBe(0);
  });

  test("standing, then walking, then standing counts only the walk", () => {
    const a = standingSamples(5, { seed: 1 });
    const w = walkingSamples(10, 1.8, { seed: 2 }).map((s) => ({ ...s, t: s.t + 5000 }));
    const b = standingSamples(5, { seed: 3 }).map((s) => ({ ...s, t: s.t + 15000 }));
    expect(Math.abs(countSteps([...a, ...w, ...b]) - 18)).toBeLessThanOrEqual(1);
  });

  test("never two steps within 300 ms", () => {
    const d = new StepDetector();
    const times: number[] = [];
    for (const s of walkingSamples(10, 1.8)) if (d.push(s)) times.push(s.t);
    for (let i = 1; i < times.length; i++) expect(times[i] - times[i - 1]).toBeGreaterThanOrEqual(300);
  });

  test("works whichever way the phone is held (gravity on another axis)", () => {
    const sideways = walkingSamples(20, 1.8).map((s) => ({ t: s.t, x: s.z, y: s.y, z: s.x }));
    expect(Math.abs(countSteps(sideways) - 36)).toBeLessThanOrEqual(1);
  });
});

describe("heading", () => {
  test("uses true heading, falls back to magnetic when ios reports -1", () => {
    expect(pickHeading({ trueHeading: 90, magHeading: 80, accuracy: 3 })).toBe(90);
    expect(pickHeading({ trueHeading: -1, magHeading: 80, accuracy: 1 })).toBe(80);
  });

  test("smoothing wraps around north", () => {
    const f = new HeadingFilter(0.5);
    f.push(350);
    const v = f.push(10);
    expect(Math.min(v, 360 - v)).toBeLessThan(10);
  });

  test("angle differences and facing hints", () => {
    expect(angleDiff(350, 10)).toBe(20);
    expect(angleDiff(10, 350)).toBe(-20);
    expect(facingHint(0, 10)).toBe("You're facing the right way.");
    expect(facingHint(0, 45)).toBe("Turn a little right.");
    expect(facingHint(0, 270)).toBe("Turn left.");
    expect(facingHint(0, 180)).toBe("Turn around.");
  });
});

describe("step queue", () => {
  test("failed posts never lose steps", () => {
    const q = new StepQueue();
    q.add(); q.add();
    expect(q.take()).toBe(2);
    q.add();
    q.fail();
    expect(q.take()).toBe(3);
    q.ack();
    expect(q.take()).toBe(0);
    expect(q.total).toBe(3);
  });

  test("step length helper rejects typos", () => {
    expect(stepLength(7)).toBe(0.7);
    expect(stepLength(70)).toBeNull();
    expect(stepLength(0)).toBeNull();
  });
});
