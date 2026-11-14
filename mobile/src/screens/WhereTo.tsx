import React, { useState } from "react";
import { ScrollView, View } from "react-native";
import { Api, ApiError, destinations, entryNode, type Graph } from "../lib/api";
import { feel, say } from "../lib/voice";
import type { Settings, Trip } from "../state";
import { Big, C, Choice, Field, H, P } from "../ui/kit";

const PROFILES = ["blind", "wheelchair", "default"] as const;
type Profile = (typeof PROFILES)[number];
const PROFILE_SAY: Record<Profile, string> = {
  blind: "blind or low vision: fewer turns, hazards avoided",
  wheelchair: "wheelchair: never stairs",
  default: "default: shortest safe route",
};

export function WhereTo({ settings, setSettings, onStarted }: {
  settings: Settings; setSettings: (s: Settings) => void; onStarted: (t: Trip) => void;
}) {
  const [sid, setSid] = useState(settings.recent[0] ?? "synthetic");
  const [graph, setGraph] = useState<Graph | null>(null);
  const [goal, setGoal] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile>("blind");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const api = new Api(settings.server);

  const report = (m: string) => { setMsg(m); say(m, settings.rate); };

  async function load(which = sid) {
    setBusy(true); setGoal(null);
    try {
      const g = await api.graph(which.trim());
      setGraph(g); setSid(which.trim());
      const places = destinations(g);
      report(`${places.length} places in this building. Pick one.`);
      const recent = [which.trim(), ...settings.recent.filter((r) => r !== which.trim())].slice(0, 5);
      setSettings({ ...settings, recent });
    } catch (e) {
      setGraph(null);
      report(e instanceof ApiError ? `Couldn't load that building: ${e.message}` : "Couldn't load that building.");
    } finally { setBusy(false); }
  }

  async function start() {
    if (!graph || !goal) return;
    const n = entryNode(graph);
    if (!n) return report("This building has no scan positions.");
    setBusy(true);
    try {
      const r = await api.start({ sid, to: goal, profile, x: n.x, y: n.y, floor: n.floor });
      const lines = r.events.map((e) => e.text).filter(Boolean).join(" ");
      say(`Starting at the ${n.room_label ?? "entrance"}. ${lines}`, settings.rate);
      r.events.forEach((e) => feel(e.vibrate, settings.haptics));
      onStarted({ api, sid, goal, profile, session: r.session, state: r.state, start: { x: n.x, y: n.y, floor: n.floor } });
    } catch (e) {
      report(e instanceof ApiError ? `Couldn't start: ${e.message}` : "Couldn't start.");
    } finally { setBusy(false); }
  }

  const places = graph ? destinations(graph) : [];
  return (
    <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: 48 }} style={{ backgroundColor: C.bg }}>
      <H>Where to?</H>
      <Field label="Building" value={sid} onChangeText={setSid} placeholder="synthetic or a Matterport id"
        returnKeyType="go" onSubmitEditing={() => load()} accessibilityHint="Type a building id, then load it" />
      <Big label={busy && !graph ? "Loading…" : "Load building"} onPress={() => load()} disabled={busy}
        hint="Fetches the building's rooms" />
      {settings.recent.length > 1 && (
        <View style={{ marginTop: 6 }}>
          <P dim>Recent</P>
          {settings.recent.map((r) => <Big key={r} label={r} onPress={() => load(r)} hint="Load this building" />)}
        </View>
      )}
      {graph && (
        <>
          <Choice label="Destination" options={places} value={(goal ?? "") as string}
            onChange={(g) => { setGoal(g); say(g, settings.rate); }} />
          <Choice label="I travel as" options={PROFILES} value={profile}
            onChange={(p) => { setProfile(p); say(PROFILE_SAY[p], settings.rate); }} describe={(p) => PROFILE_SAY[p]} />
          {graph.warnings.length > 0 && <P dim>Map notes: {graph.warnings.join("; ")}</P>}
          <Big kind="primary" label={goal ? `Start to ${goal}` : "Pick a destination"} onPress={start}
            disabled={!goal || busy} hint="Starts guidance from the building entrance" />
        </>
      )}
      {!!msg && <View accessibilityLiveRegion="polite"><P>{msg}</P></View>}
    </ScrollView>
  );
}
