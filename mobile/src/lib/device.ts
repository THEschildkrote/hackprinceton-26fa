// the haptic band line protocol, mirroring nudge/device.py. every line fits one ble packet (< 20 bytes).
//   route step:  "<i>,<act>,<dir>,<deg>,<m>"     e.g. "2,T,L,90,4.5"
//   floor step:  "<i>,F,<U|D>,<E|S|X>,<floor#>"   e.g. "6,F,U,E,1"
//   live:        "N,<m_next>,<deg_next>,<act><dir>,<flags>"  e.g. "N,2.4,-15,TR,0"
//                flags: bit0 off route, bit1 arrived, bit2 hazard ahead (reserved). deg is "" when heading unknown.
// metres everywhere; degrees are model frame, + = left.

export type Act = "depart" | "turn" | "bear" | "uturn" | "straight" | "walk" | "floor" | "arrive";
export type Dir = "L" | "R" | "S";

export const ACT: Record<Act, string> = {
  depart: "D", turn: "T", bear: "B", uturn: "U", straight: "S", walk: "W", floor: "F", arrive: "A",
};
const ACT_BACK: Record<string, Act> = Object.fromEntries(Object.entries(ACT).map(([k, v]) => [v, k as Act]));
const VIA: Record<string, string> = { elevator: "E", stairs: "S" };
const VIA_BACK: Record<string, string> = { E: "elevator", S: "stairs", X: "unknown" };
export const MAX_M = 999.9;

export interface RouteStep {
  i: number; act: Act; dir: Dir; deg: number; m: number;
  up?: boolean; via?: string; floor_i?: number; rise?: number;
}

export interface LivePacket {
  m_next: number | null; deg_next: number | null; act_next: Act | null; dir_next: Dir;
  off: boolean; done: boolean; hazard?: boolean;
}

export const metres = (x: number) => Math.round(Math.min(MAX_M, Math.max(0, x)) * 10) / 10;

// python formats 3.0 as "3.0"; keep the same spelling so lines compare byte for byte
const fmtM = (x: number) => { const v = metres(x); return Number.isInteger(v) ? v.toFixed(1) : String(v); };

export function encodeStep(p: RouteStep): string {
  const up = p.rise != null ? p.rise >= 0 : p.up !== false;
  const s = p.act === "floor"
    ? `${p.i},F,${up ? "U" : "D"},${VIA[p.via ?? ""] ?? "X"},${p.floor_i ?? 0}`
    : `${p.i},${ACT[p.act]},${p.dir},${Math.trunc(p.deg)},${fmtM(p.m)}`;
  if (s.length >= 20) throw new Error(`line too long: ${s}`);
  return s;
}

export function decodeStep(s: string): RouteStep | null {
  const f = s.trim().split(",");
  if (f.length !== 5) return null;
  const i = parseInt(f[0], 10), act = ACT_BACK[f[1]];
  if (Number.isNaN(i) || !act) return null;
  if (act === "floor") {
    if (!(f[2] === "U" || f[2] === "D") || !(f[3] in VIA_BACK)) return null;
    return { i, act, dir: "S", deg: 0, m: 0, up: f[2] === "U", via: VIA_BACK[f[3]], floor_i: parseInt(f[4], 10) };
  }
  if (!(f[2] === "L" || f[2] === "R" || f[2] === "S")) return null;
  const deg = parseInt(f[3], 10), m = parseFloat(f[4]);
  if (Number.isNaN(deg) || Number.isNaN(m)) return null;
  return { i, act, dir: f[2] as Dir, deg, m };
}

export function encodeLive(p: LivePacket): string {
  const m = p.m_next == null ? "" : fmtM(p.m_next);
  const d = p.deg_next == null ? "" : String(Math.trunc(p.deg_next));
  const act = p.act_next ? ACT[p.act_next] ?? "-" : "-";
  const flags = (p.off ? 1 : 0) | (p.done ? 2 : 0) | (p.hazard ? 4 : 0);
  const s = `N,${m},${d},${act}${p.dir_next},${flags}`;
  if (s.length >= 20) throw new Error(`line too long: ${s}`);
  return s;
}

export function decodeLive(s: string): LivePacket | null {
  const f = s.trim().split(",");
  if (f[0] !== "N" || f.length !== 5 || f[3].length !== 2) return null;
  const fl = parseInt(f[4], 10);
  if (Number.isNaN(fl)) return null;
  return {
    m_next: f[1] === "" ? null : parseFloat(f[1]),
    deg_next: f[2] === "" ? null : parseInt(f[2], 10),
    act_next: ACT_BACK[f[3][0]] ?? null,
    dir_next: f[3][1] as Dir,
    off: !!(fl & 1), done: !!(fl & 2), hazard: !!(fl & 4),
  };
}

// ble ids, copied from firmware/README.md
export const BAND = {
  service: "7a6e0001-1d6e-4c1a-9b5e-6e7564676500",
  rx: "7a6e0002-1d6e-4c1a-9b5e-6e7564676500", // phone writes lines
  tx: "7a6e0003-1d6e-4c1a-9b5e-6e7564676500", // band notifies "B,repeat" / "B,lost"
};

export function parseButton(s: string): "repeat" | "lost" | null {
  const t = s.trim();
  return t === "B,repeat" ? "repeat" : t === "B,lost" ? "lost" : null;
}

// ble characteristic values are base64; our lines are plain ascii
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
export function asciiToB64(s: string): string {
  let out = "";
  for (let i = 0; i < s.length; i += 3) {
    const [a, b, c] = [s.charCodeAt(i), s.charCodeAt(i + 1), s.charCodeAt(i + 2)];
    const n = (a << 16) | ((b || 0) << 8) | (c || 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + (i + 1 < s.length ? B64[(n >> 6) & 63] : "=") + (i + 2 < s.length ? B64[n & 63] : "=");
  }
  return out;
}
export function b64ToAscii(b: string): string {
  let out = "", buf = 0, bits = 0;
  for (const ch of b.replace(/=+$/, "")) {
    const v = B64.indexOf(ch);
    if (v < 0) continue;
    buf = (buf << 6) | v; bits += 6;
    if (bits >= 8) { bits -= 8; out += String.fromCharCode((buf >> bits) & 255); }
  }
  return out;
}
