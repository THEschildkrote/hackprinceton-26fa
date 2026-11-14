// typed client for nudge/server.py and nudge/device_api.py. all distances are metres.
import type { Point } from "./sim";
import type { VoiceReply } from "./talk";

export interface NavEvent { type: string; text?: string; cue?: string | null; vibrate?: number[] | null }
export interface Maneuver { type: string; text: string; along: number; distance: number; side?: string | null; turn_deg?: number; cue?: string }
export interface NavState {
  route: { nodes: string[]; length: number; points: Point[]; profile: string } | null;
  progress?: number; remaining?: number;
  next?: { type: string; text: string; along: number; cue?: string } | null;
  distance_to_next_m?: number | null; bearing_to_next_deg?: number | null;
  maneuvers?: Maneuver[]; done?: boolean; reroutes?: number; next_edge?: [string, string];
}
export interface DeviceInfo { packet: unknown; line: string | null }
export interface NavReply { events: NavEvent[]; state: NavState; device?: DeviceInfo }
export interface StartReply extends NavReply { session: string }
export interface GraphNode { id: string; label: string; x: number; y: number; z: number; floor: string; room: string | null; room_label: string | null; tags: string[] }
export interface Graph { sid: string; floors: { id: string; label: string }[]; nodes: GraphNode[]; warnings: string[] }
export interface Pending { name: string; args: Record<string, unknown>; description?: string }
export interface ExceptionReply extends Partial<NavReply> { mode: "agent" | "fallback"; reason?: string; say: string[]; pending: Pending | null }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export class Api {
  constructor(public base: string, private timeoutMs = 10000) {}

  private async req<T>(method: "GET" | "POST", path: string, body?: unknown, timeoutMs = this.timeoutMs): Promise<T> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(this.base.replace(/\/+$/, "") + path, {
        method, signal: ctrl.signal,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!r.ok) {
        let msg = `${r.status}`;
        try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j); } catch { /* not json */ }
        throw new ApiError(r.status, msg);
      }
      return (await r.json()) as T;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      throw new ApiError(0, `can't reach the server at ${this.base}`);
    } finally {
      clearTimeout(timer);
    }
  }

  profiles() { return this.req<Record<string, unknown>>("GET", "/api/profiles"); }
  graph(sid: string) { return this.req<Graph>("GET", `/api/graph/${encodeURIComponent(sid)}`); }

  start(b: { sid: string; to: string; profile: string; x: number; y: number; floor: string; heading?: number | null }) {
    return this.req<StartReply>("POST", "/api/nav/start", b);
  }
  update(b: { session: string; x: number; y: number; floor: string; heading?: number | null }) {
    return this.req<NavReply>("POST", "/api/nav/update", b);
  }
  block(b: { session: string; x: number; y: number; floor: string; a: string; b?: string | null; heading?: number | null }) {
    return this.req<NavReply>("POST", "/api/nav/block", b);
  }
  // the exception agent reasons with an llm (free models can take ~30 s), so these get a long timeout
  exception(session: string, text: string) {
    return this.req<ExceptionReply>("POST", "/api/nav/exception", { session, text }, 90000);
  }
  decide(session: string, decision: "approve" | "reject") {
    return this.req<ExceptionReply>("POST", "/api/nav/exception/decide", { session, decision }, 90000);
  }

  // voice turns (nudge/voice.py); agent-backed answers can take a while on free models
  voiceTurn(b: { traveller: string; text: string; sid?: string | null; session?: string | null; x?: number; y?: number; floor?: string; profile?: string }) {
    return this.req<VoiceReply>("POST", "/api/voice/turn", b, 90000);
  }
  voicePosition(b: { traveller: string; x: number; y: number; floor: string; heading?: number | null }) {
    return this.req<VoiceReply & { events?: NavEvent[] }>("POST", "/api/voice/position", b);
  }
  // caretaker pairing (nudge/hub.py); returns the code the traveller reads to their caretaker
  registerTraveller(traveller: string, name?: string | null) {
    return this.req<{ code?: string }>("POST", "/api/traveller/register", { traveller, name: name ?? null });
  }

  // haptic band relay (nudge/device_api.py)
  deviceRoute(session: string) { return this.req<{ lines: string[]; reroutes: number }>("GET", `/api/device/${session}/route`); }
  deviceLive(session: string) { return this.req<{ line: string; seq: number; say: string; reroutes: number }>("GET", `/api/device/${session}/live`); }
  deviceButton(session: string, event: "repeat" | "lost") { return this.req<{ say: string }>("POST", `/api/device/${session}/button`, { event }); }
}

// where to start a trip: the pano in a lobby/entrance-like room, else the first one (mirrors agents/tools.key_places)
const ENTRY = ["lobby", "entr", "foyer", "reception", "vestibule"];
export function entryNode(g: Graph): GraphNode | null {
  const byRoom = g.nodes.filter((n) => n.room_label && ENTRY.some((w) => n.room_label!.toLowerCase().includes(w)));
  const pool = byRoom.length ? byRoom : g.nodes.filter((n) => !g.floors.length || n.floor === g.floors[0].id);
  if (!pool.length) return g.nodes[0] ?? null;
  const cx = pool.reduce((s, n) => s + n.x, 0) / pool.length, cy = pool.reduce((s, n) => s + n.y, 0) / pool.length;
  return pool.reduce((best, n) => (Math.hypot(n.x - cx, n.y - cy) < Math.hypot(best.x - cx, best.y - cy) ? n : best));
}

export function destinations(g: Graph): string[] {
  return [...new Set(g.nodes.map((n) => n.room_label).filter((l): l is string => !!l && l !== "Void"))].sort();
}

// spoken/visible metres: half-metre steps up close, whole metres beyond 10 (matches nudge/instruct.meters)
export function fmtMetres(m: number | null | undefined): string {
  if (m == null || Number.isNaN(m)) return "-";
  if (m < 0.75) return "< 1 m";
  const v = m < 10 ? Math.round(m * 2) / 2 : Math.round(m);
  return `${v} m`;
}

// plain words for a pending reroute, from the commit_reroute tool arguments
export function describePending(p: Pending): string {
  const a = p.args ?? {};
  const parts: string[] = [];
  if (a.block_node) parts.push(`avoid the ${String(a.block_node).replace(/^p_/, "")}`);
  if (a.block_edge) parts.push("avoid that passage");
  if (a.profile) parts.push(`plan ${a.profile} routes`);
  if (a.goal) parts.push(`${parts.length ? "and go" : "go"} to the ${a.goal}`);
  const what = parts.length ? parts.join(" ") : "change the route";
  const reason = a.reason ? String(a.reason).trim().replace(/[.!?]+$/, "") : "";
  return `New route: ${what}.${reason ? ` Reason: ${reason}.` : ""}`;
}
