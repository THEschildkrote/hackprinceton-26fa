import React, { useEffect, useRef, useState } from "react";
import { ScrollView, View } from "react-native";
import { Api, ApiError, buildingLabel, entryNode, placeNames, type Building, type Graph } from "../lib/api";
import { feel, say } from "../lib/voice";
import type { Settings, Trip } from "../state";
import { Big, C, Choice, Field, H, P } from "../ui/kit";
import { TalkBar } from "../ui/TalkBar";
import { tripFromReply, type VoiceReply } from "../lib/talk";

// what the traveller sees; the api still gets blind / wheelchair / default
const PROFILES = ["I use a cane", "I use a wheelchair", "I'm sighted"] as const;
type Profile = (typeof PROFILES)[number];
const PROFILE_API: Record<Profile, string> = { "I use a cane": "blind", "I use a wheelchair": "wheelchair", "I'm sighted": "default" };
const PROFILE_SAY: Record<Profile, string> = {
  "I use a cane": "I use a cane: fewer turns, hazards avoided",
  "I use a wheelchair": "I use a wheelchair: never stairs or narrow doors",
  "I'm sighted": "I'm sighted: shortest safe route",
};
const SHOW = 12;  // destinations before "More"

export function WhereTo({ settings, setSettings, onStarted }: {
  settings: Settings; setSettings: (s: Settings) => void; onStarted: (t: Trip) => void;
}) {
  const [building, setBuilding] = useState<Building | null>(settings.recent[0] ?? null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [places, setPlaces] = useState<string[]>([]);
  const [more, setMore] = useState(false);
  const [goal, setGoal] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile>("I use a cane");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Building[]>([]);
  const [searching, setSearching] = useState(false);  // the building picker is open
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const api = new Api(settings.server);
  const sid = building?.sid ?? null;
  const report = (m: string) => { setMsg(m); say(m, settings.rate); };

  // open the remembered building (the demo building on first launch)
  useEffect(() => { if (building) open(building, false); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  // search as you type, a beat after the last key
  useEffect(() => {
    if (!searching) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try { setResults((await api.buildings(query.trim())).results); }
      catch (e) { setResults([]); setMsg(e instanceof ApiError ? e.message : "Couldn't search buildings."); }
    }, 250);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [query, searching]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function open(b: Building, announce = true) {
    setBusy(true); setGoal(null); setMore(false); setSearching(false); setBuilding(b);
    try {
      const [g, cat] = await Promise.all([api.graph(b.sid), api.places(b.sid).catch(() => ({ places: [] }))]);
      setGraph(g);
      const names = placeNames(cat.places);
      setPlaces(names.length ? names : [...new Set(g.nodes.map((n) => n.room_label).filter((l): l is string => !!l && l !== "Void"))].sort());
      const recent = [b, ...settings.recent.filter((r) => r.sid !== b.sid)].slice(0, 5);
      setSettings({ ...settings, recent });
      if (announce) report(`${buildingLabel(b)}. Where would you like to go? Say it, or pick a place.`);
      else setMsg("");
    } catch (e) {
      setGraph(null); setPlaces([]);
      report(e instanceof ApiError ? `Couldn't open that building: ${e.message}` : "Couldn't open that building.");
    } finally { setBusy(false); }
  }

  async function start() {
    if (!graph || !goal || !sid) return;
    const n = entryNode(graph);
    if (!n) return report("This building has no scan positions.");
    setBusy(true);
    try {
      const r = await api.start({ sid, to: goal, profile: PROFILE_API[profile], x: n.x, y: n.y, floor: n.floor });
      const lines = r.events.map((e) => e.text).filter(Boolean).join(" ");
      say(`Starting at the ${n.room_label ?? "entrance"}. ${lines}`, settings.rate);
      r.events.forEach((e) => feel(e.vibrate, settings.haptics));
      onStarted({ api, sid, goal, profile: PROFILE_API[profile], session: r.session, state: r.state, start: { x: n.x, y: n.y, floor: n.floor } });
    } catch (e) {
      report(e instanceof ApiError ? `Couldn't start: ${e.message}` : "Couldn't start.");
    } finally { setBusy(false); }
  }

  // a trip started by voice: pick up its full state and switch to the navigate screen
  async function voiceStarted(r: VoiceReply) {
    const t = tripFromReply(r);
    if (!t) return;
    try {
      const full = await api.update({ session: t.session, x: t.start.x, y: t.start.y, floor: t.start.floor });
      onStarted({ api, sid: t.sid, goal: t.goal, profile: PROFILE_API[profile], session: t.session, state: full.state, start: t.start });
    } catch {
      report("I started your trip, but couldn't open the navigation screen.");
    }
  }

  const shown = more ? places : places.slice(0, SHOW);
  return (
    <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: 48 }} style={{ backgroundColor: C.bg }}
      keyboardShouldPersistTaps="handled">
      <H>Where to?</H>

      {!searching && building && (
        <View style={{ marginBottom: 8 }} accessible accessibilityLabel={`Building: ${buildingLabel(building)}`}>
          <P dim>Building</P>
          <P>{buildingLabel(building)}</P>
          <Big label="Change building" onPress={() => { setSearching(true); setQuery(""); }}
            hint="Search another address or building" />
        </View>
      )}

      {(searching || !building) && (
        <View style={{ marginBottom: 8 }}>
          <Field label="Search an address or building" value={query} onChangeText={setQuery}
            placeholder="e.g. 1255 Atlantic, Houston, Broadway" returnKeyType="search" autoFocus
            accessibilityHint="Results update as you type" />
          {results.length === 0 && query.trim() !== "" && <P dim>No buildings match that.</P>}
          {results.map((b) => (
            <Big key={b.sid} label={buildingLabel(b)} onPress={() => open(b)}
              hint={`${b.rooms ?? 0} rooms${b.floors ? `, ${b.floors} floors` : ""}. Opens this building`} />
          ))}
          {building && <Big label="Cancel" onPress={() => setSearching(false)} hint="Keep the current building" />}
        </View>
      )}

      {!searching && sid && (
        <>
          <TalkBar settings={settings} sid={sid} onReply={voiceStarted}
            prompt="Say where you want to go, like: take me to the bathroom. Or pick below." />
          {busy && !graph && <P dim>Opening the building…</P>}
          {graph && places.length > 0 && (
            <>
              <Choice label="Destination" options={shown} value={(goal ?? "") as string}
                onChange={(g) => { setGoal(g); say(g, settings.rate); }} />
              {places.length > SHOW && (
                <Big label={more ? "Fewer places" : `More places (${places.length - SHOW})`} onPress={() => setMore(!more)}
                  hint="Shows hallways, closets and other spaces" />
              )}
              <Choice label="I travel as" options={PROFILES} value={profile}
                onChange={(p) => { setProfile(p); say(PROFILE_SAY[p], settings.rate); }} describe={(p) => PROFILE_SAY[p]} />
              <Big kind="primary" label={goal ? `Start to ${goal}` : "Pick a destination"} onPress={start}
                disabled={!goal || busy} hint="Starts guidance from the building entrance" />
            </>
          )}
          {settings.recent.length > 1 && (
            <View style={{ marginTop: 12 }}>
              <P dim>Recent buildings</P>
              {settings.recent.filter((r) => r.sid !== sid).map((r) => (
                <Big key={r.sid} label={buildingLabel(r)} onPress={() => open(r)} hint="Open this building" />
              ))}
            </View>
          )}
        </>
      )}
      {!!msg && <View accessibilityLiveRegion="polite"><P>{msg}</P></View>}
    </ScrollView>
  );
}
