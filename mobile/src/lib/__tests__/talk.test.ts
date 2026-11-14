import { newTravellerId, QUICK_PHRASES, speechAvailable, tripFromReply, type VoiceReply } from "../talk";

describe("voice helpers", () => {
  test("no trip in the reply means no navigate screen", () => {
    expect(tripFromReply({ say: "Where to?" })).toBeNull();
    expect(tripFromReply({ say: "x", session: "s1" })).toBeNull();
  });

  test("a voice-started trip becomes an app trip", () => {
    const r: VoiceReply = { say: "Okay, going to the Kitchen.", session: "s1",
      trip: { goal: "Kitchen", sid: "synthetic", start: [5, 1.5, "f1"] } };
    expect(tripFromReply(r)).toEqual({ session: "s1", sid: "synthetic", goal: "Kitchen", start: { x: 5, y: 1.5, floor: "f1" } });
  });

  test("falls back to the first path point for the start", () => {
    const r: VoiceReply = { say: "ok", session: "s2", trip: { goal: "Office", sid: "synthetic", start: null },
      state: { path: [[2, 3, "f1"], [4, 3, "f1"]] } };
    expect(tripFromReply(r)?.start).toEqual({ x: 2, y: 3, floor: "f1" });
  });

  test("speech availability never throws and quick phrases include help", () => {
    expect(typeof speechAvailable()).toBe("boolean");
    expect(QUICK_PHRASES).toContain("I need help");
    expect(newTravellerId()).toMatch(/^app-[a-z0-9]+$/);
  });
});
