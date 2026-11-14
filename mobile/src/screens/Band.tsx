import React, { useEffect, useRef, useState } from "react";
import { ScrollView, Text, View } from "react-native";
import { BandLink, bleSupport } from "../lib/band";
import { decodeLive } from "../lib/device";
import { BandEngine, L, R } from "../lib/haptics";
import { say } from "../lib/voice";
import type { Settings, Trip } from "../state";
import { Big, C, Choice, H, P } from "../ui/kit";

const MODES = ["simulated band", "bluetooth band"] as const;
type Mode = (typeof MODES)[number];

function Motor({ side, on }: { side: string; on: boolean }) {
  return (
    <View accessible accessibilityLabel={`${side} motor ${on ? "on" : "off"}`}
      style={{ flex: 1, height: 140, borderRadius: 70, borderWidth: 4, alignItems: "center", justifyContent: "center",
        borderColor: on ? C.accent : C.line, backgroundColor: on ? C.accent : C.card }}>
      {/* the word changes too, not only the colour */}
      <Text style={{ fontSize: 26, fontWeight: "800", color: on ? C.accentText : C.dim }}>{side}{on ? " ●" : ""}</Text>
    </View>
  );
}

export function Band({ trip, settings }: { trip: Trip | null; settings: Settings }) {
  const [mode, setMode] = useState<Mode>("simulated band");
  const [motors, setMotors] = useState(0);
  const [line, setLine] = useState("");
  const [status, setStatus] = useState("");
  const [linked, setLinked] = useState(false);
  const engine = useRef(new BandEngine());
  const link = useRef(new BandLink());
  const seen = useRef({ seq: -1, reroutes: -1 });

  // poll the server's live line; feed the simulated band or forward it to the real one
  useEffect(() => {
    if (!trip) return;
    let alive = true;
    const id = setInterval(async () => {
      try {
        const r = await trip.api.deviceLive(trip.session);
        if (!alive) return;
        if (r.reroutes !== seen.current.reroutes) {
          seen.current.reroutes = r.reroutes;
          if (link.current.connected) {
            const route = await trip.api.deviceRoute(trip.session);
            for (const l of route.lines) await link.current.write(l);
          }
        }
        if (r.seq !== seen.current.seq) {
          seen.current.seq = r.seq;
          setLine(r.line);
          const p = decodeLive(r.line);
          if (p) engine.current.onLive(p, Date.now());
          if (link.current.connected) link.current.write(r.line);
        }
      } catch (e) {
        if (alive) setStatus("The server has no band feed for this trip yet (needs /api/device).");
      }
    }, 400);
    return () => { alive = false; clearInterval(id); };
  }, [trip]);

  // drive the two motor indicators from the same grammar the firmware runs
  useEffect(() => {
    if (mode !== "simulated band") return;
    const id = setInterval(() => setMotors(engine.current.tick(Date.now())), 20);
    return () => clearInterval(id);
  }, [mode]);

  async function connect() {
    setStatus("Looking for the band…");
    try {
      const name = await link.current.connect(async (b) => {
        if (!trip) return;
        try { say((await trip.api.deviceButton(trip.session, b)).say, settings.rate); } catch { /* offline */ }
      });
      setLinked(true); seen.current.reroutes = -1; // resend the route
      setStatus(`Connected to ${name}.`); say(`Band connected.`, settings.rate);
    } catch (e) {
      setStatus(String((e as Error).message)); say(String((e as Error).message), settings.rate);
    }
  }

  const sup = bleSupport();
  return (
    <ScrollView style={{ backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 48 }}>
      <H>Haptic band</H>
      {!trip && <P dim>Start a trip first; the band follows the active route.</P>}
      <Choice label="Band" options={MODES} value={mode} onChange={setMode} />
      {mode === "simulated band" ? (
        <>
          <P dim>Same pulses the real band plays: left motor for left, right for right, faster pulses mean turn more.</P>
          <View style={{ flexDirection: "row", gap: 16, marginVertical: 12 }}>
            <Motor side="Left" on={!!(motors & L)} />
            <Motor side="Right" on={!!(motors & R)} />
          </View>
        </>
      ) : sup.ok ? (
        linked ? <Big label="Disconnect band" onPress={async () => { await link.current.disconnect(); setLinked(false); setStatus("Disconnected."); }} />
          : <Big kind="primary" label="Connect band" hint="Scans for the Nudge band over Bluetooth" onPress={connect} />
      ) : (
        <P>{sup.why}</P>
      )}
      <P dim>Last line sent: {line || "none yet"}</P>
      {!!status && <View accessibilityLiveRegion="polite"><P>{status}</P></View>}
    </ScrollView>
  );
}
