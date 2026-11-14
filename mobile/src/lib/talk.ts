// talking to nudge: speech to text (dev build or web), one voice turn on the server (nudge/voice.py).
// expo go has no speech recognition module, so everything here degrades to typed text.

export interface VoiceReply {
  say: string;
  session?: string | null;
  pending?: { name: string; args: Record<string, unknown> } | null;
  choice?: string[] | null;
  trip?: { goal: string; sid: string; start: [number, number, string] | null } | null;
  state?: { remaining?: number; next?: { text?: string; cue?: string } | null; path?: [number, number, string][];
            unmapped?: boolean; done?: boolean } | null;
}

export const QUICK_PHRASES = ["Where am I", "Repeat", "How far", "I need help"] as const;

type SR = {
  requestPermissionsAsync(): Promise<{ granted: boolean }>;
  start(o: { lang: string; interimResults: boolean; maxAlternatives?: number; continuous?: boolean }): void;
  stop(): void;
  abort(): void;
  addListener(ev: string, fn: (e: any) => void): { remove(): void };
};

let cached: SR | null | undefined;

// the native module only exists in a dev build (or as a web speech shim on web); never crash without it
export function speechModule(): SR | null {
  if (cached !== undefined) return cached;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const m = require("expo-speech-recognition");
    cached = (m?.ExpoSpeechRecognitionModule as SR) ?? null;
  } catch {
    cached = null;
  }
  return cached;
}

export function speechAvailable(): boolean {
  return speechModule() !== null;
}

// listen until stop() is called (or the recogniser ends); resolves with the best transcript, "" if nothing
export function listen(lang = "en-US"): { done: Promise<string>; stop: () => void } {
  const sr = speechModule();
  if (!sr) return { done: Promise.resolve(""), stop: () => {} };
  let text = "";
  const subs: { remove(): void }[] = [];
  const done = new Promise<string>((resolve) => {
    const finish = () => { subs.forEach((s) => s.remove()); resolve(text.trim()); };
    subs.push(sr.addListener("result", (e) => {
      const best = e?.results?.[0]?.transcript;
      if (typeof best === "string") text = best;
    }));
    subs.push(sr.addListener("end", finish));
    subs.push(sr.addListener("error", finish));
    sr.requestPermissionsAsync()
      .then((p) => (p.granted ? sr.start({ lang, interimResults: true, maxAlternatives: 1 }) : finish()))
      .catch(finish);
  });
  return { done, stop: () => { try { sr.stop(); } catch { /* already stopped */ } } };
}

// a voice reply that started a trip becomes the app's Trip (navigate screen, band, caretaker all follow it)
export function tripFromReply(r: VoiceReply): { session: string; sid: string; goal: string; start: { x: number; y: number; floor: string } } | null {
  if (!r.session || !r.trip) return null;
  const p = r.trip.start ?? r.state?.path?.[0] ?? null;
  if (!p) return null;
  return { session: r.session, sid: r.trip.sid, goal: r.trip.goal, start: { x: p[0], y: p[1], floor: p[2] } };
}

// local traveller id, stable per install
export function newTravellerId(): string {
  return "app-" + Math.random().toString(36).slice(2, 10);
}
