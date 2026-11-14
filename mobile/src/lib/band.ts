// ble link to the haptic band (firmware/nudge_band). react-native-ble-plx is a native module: it works in a
// development build (npx expo run:android / eas build --profile development), not in expo go or on the web.
import Constants from "expo-constants";
import { PermissionsAndroid, Platform } from "react-native";
import { asciiToB64, b64ToAscii, BAND, parseButton } from "./device";

export function bleSupport(): { ok: boolean; why?: string } {
  if (Platform.OS === "web") return { ok: false, why: "Bluetooth isn't available in the web build. On Android Chrome, open companion.html from the server instead." };
  if (Constants.executionEnvironment === "storeClient") return { ok: false, why: "Expo Go can't use Bluetooth. Install a development build of this app to connect the band." };
  try {
    require("react-native-ble-plx");
    return { ok: true };
  } catch {
    return { ok: false, why: "The Bluetooth module isn't in this build. Rebuild the app with react-native-ble-plx." };
  }
}

type Device = {
  connect(): Promise<Device>;
  discoverAllServicesAndCharacteristics(): Promise<Device>;
  writeCharacteristicWithResponseForService(s: string, c: string, v: string): Promise<unknown>;
  monitorCharacteristicForService(s: string, c: string, cb: (e: unknown, ch: { value?: string | null } | null) => void): { remove(): void };
  cancelConnection(): Promise<unknown>;
  name?: string | null;
};

async function androidPermissions() {
  if (Platform.OS !== "android") return true;
  const P = PermissionsAndroid.PERMISSIONS;
  const want = [P.BLUETOOTH_SCAN, P.BLUETOOTH_CONNECT, P.ACCESS_FINE_LOCATION].filter(Boolean);
  const res = await PermissionsAndroid.requestMultiple(want);
  return Object.values(res).every((v) => v === PermissionsAndroid.RESULTS.GRANTED);
}

export class BandLink {
  private manager: { startDeviceScan: Function; stopDeviceScan: Function; destroy: Function } | null = null;
  private device: Device | null = null;
  private sub: { remove(): void } | null = null;
  private queue: Promise<unknown> = Promise.resolve();

  get connected() { return !!this.device; }

  async connect(onButton: (b: "repeat" | "lost") => void, timeoutMs = 12000): Promise<string> {
    const sup = bleSupport();
    if (!sup.ok) throw new Error(sup.why);
    if (!(await androidPermissions())) throw new Error("Bluetooth permission was denied.");
    const { BleManager } = require("react-native-ble-plx");
    this.manager = this.manager ?? new BleManager();
    const found: Device = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.manager!.stopDeviceScan(); reject(new Error("No band found nearby. Is it switched on?")); }, timeoutMs);
      this.manager!.startDeviceScan([BAND.service], null, (err: Error | null, d: Device | null) => {
        if (err) { clearTimeout(timer); this.manager!.stopDeviceScan(); reject(err); return; }
        if (d) { clearTimeout(timer); this.manager!.stopDeviceScan(); resolve(d); }
      });
    });
    const d = await (await found.connect()).discoverAllServicesAndCharacteristics();
    this.device = d;
    this.sub = d.monitorCharacteristicForService(BAND.service, BAND.tx, (_e, ch) => {
      const b = ch?.value ? parseButton(b64ToAscii(ch.value)) : null;
      if (b) onButton(b);
    });
    return d.name ?? "Nudge band";
  }

  // writes are serialised so lines arrive in order
  write(line: string) {
    const d = this.device;
    if (!d) return Promise.resolve();
    this.queue = this.queue.then(() => d.writeCharacteristicWithResponseForService(BAND.service, BAND.rx, asciiToB64(line))).catch(() => {});
    return this.queue;
  }

  async disconnect() {
    this.sub?.remove(); this.sub = null;
    try { await this.device?.cancelConnection(); } catch { /* already gone */ }
    this.device = null;
  }
}
