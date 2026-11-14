import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";
import { Platform } from "react-native";
import type { Api, Building, NavState } from "./lib/api";

export interface Settings {
  server: string;
  rate: number;        // speech rate, 1 = normal
  haptics: boolean;
  recent: Building[];  // last buildings, newest first (shown by address)
  traveller: string;   // stable id for voice turns and caretaker pairing
  caretakerCode: string | null;  // code the caretaker texts to follow this traveller
}

// web: same origin as the page (served next to the api or behind a proxy), or localhost:8765 from the expo dev
// server. phone: the laptop that serves the bundle also runs the api, so reuse the dev server's host.
function defaultServer() {
  if (Platform.OS === "web" && typeof window !== "undefined" && window.location) {
    const dev = ["8081", "19006"].includes(window.location.port);
    return dev ? "http://localhost:8765" : window.location.origin;
  }
  const host = Constants.expoConfig?.hostUri?.split(":")[0];
  if (host && !host.includes(".exp.direct")) return `http://${host}:8765`;
  return "http://192.168.1.10:8765";  // fallback; change it in settings
}
export const DEFAULT_SERVER = defaultServer();

// the demo building: real matterport scan, vexcel outdoors, costar record
export const DEMO_BUILDING: Building = { sid: "MgGNVWsyXtH", address: "1255 S Atlantic Blvd", city: "Los Angeles" };

export const DEFAULTS: Settings = { server: DEFAULT_SERVER, rate: 1.0, haptics: true, recent: [DEMO_BUILDING],
  traveller: "", caretakerCode: null };

const KEY = "nudge.settings.v2";  // v2: recent holds buildings, not raw ids

export async function loadSettings(): Promise<Settings> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return DEFAULTS;
    const s = { ...DEFAULTS, ...JSON.parse(raw) } as Settings;
    const recent = (s.recent ?? []).filter((b) => b && typeof b === "object" && b.sid);
    return { ...s, recent: recent.length ? recent : [DEMO_BUILDING] };
  } catch {
    return DEFAULTS;
  }
}

export async function saveSettings(s: Settings) {
  try { await AsyncStorage.setItem(KEY, JSON.stringify(s)); } catch { /* storage is a convenience */ }
}

export interface Trip {
  api: Api;
  sid: string;
  goal: string;
  profile: string;
  session: string;
  state: NavState;
  start: { x: number; y: number; floor: string };
}
