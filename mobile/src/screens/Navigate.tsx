import { Magnetometer, Pedometer } from "expo-sensors";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Modal, ScrollView, Text, View } from "react-native";
import { ApiError, describePending, fmtMetres, type ExceptionReply, type NavReply, type NavState, type Pending } from "../lib/api";
import { decodeLive } from "../lib/device";
import { BandEngine, L, R } from "../lib/haptics";
import { calibrateOffset, modelHeading, StepTracker } from "../lib/pdr";
import { SimWalk, type Pose } from "../lib/sim";
import { tripFromReply, type VoiceReply } from "../lib/talk";
import { feel, say, stopSpeaking } from "../lib/voice";
import type { Settings, Trip } from "../state";
import { Big, C, Field, H, P } from "../ui/kit";
import { TalkBar } from "../ui/TalkBar";

const PROBLEMS = ["Elevator out", "Door locked", "I'm lost"] as const;

// a small left/right strip that pulses with the same grammar as the band, so the haptics are visible mid-walk
function MotorStrip({ motors }: { motors: number }) {
  const pill = (side: string, on: boolean) => (
    <View key={side} style={{ flex: 1, height: 40, borderRadius: 20, borderWidth: 3, alignItems: "center", justifyContent: "center",
      borderColor: on ? C.accent : C.line, backgroundColor: on ? C.accent : C.card }}>
      <Text style={{ fontSize: 18, fontWeight: "800", color: on ? C.accentText : C.dim }}>{side}{on ? " ●" : ""}</Text>
    </View>
  );
  return (
    <View accessible accessibilityLabel={`Haptic band: left ${motors & L ? "on" : "off"}, right ${motors & R ? "on" : "off"}`}
      style={{ flexDirection: "row", gap: 12, marginVertical: 8 }}>
      {pill("Left", !!(motors & L))}
      {pill("Right", !!(motors & R))}
    </View>
  );
}

function direction(bearing: number | null | undefined): string | null {
  if (bearing == null) return null;
  const a = Math.round(Math.abs(bearing));
  if (a <= 15) return "Straight ahead";
  if (a >= 160) return "Turn around";
  return `${a} degrees to your ${bearing > 0 ? "left" : "right"}`;
}

export function Navigate({ trip, settings, onEnd, onNewTrip }: {
  trip: Trip; settings: Settings; onEnd: () => void; onNewTrip: (t: Trip) => void;
}) {
  const { api, session } = trip;
  const [nav, setNav] = useState<NavState>(trip.state);
  const [instr, setInstr] = useState(trip.state.next?.text ?? "Starting.");
  const source = settings.posSource;
  const [running, setRunning] = useState(false);
  const [motors, setMotors] = useState(0);
  const engine = useRef(new BandEngine());
  const [problemOpen, setProblemOpen] = useState(false);
  const [freeText, setFreeText] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [note, setNote] = useState("");
  const [thinking, setThinking] = useState(false);

  const sim = useRef(new SimWalk(trip.state.route?.points ?? [], 1.1));
  const tracker = useRef(new StepTracker(trip.start.x, trip.start.y, trip.start.floor));
  const mag = useRef<{ x: number; y: number } | null>(null);
  const offset = useRef<number | null>(null);
  const busy = useRef(false);
  const pose = useRef<Pose>({ ...trip.start, heading: 0, done: false });

  const speak = useCallback((t: string) => { setInstr(t); say(t, settings.rate); }, [settings.rate]);

  const applyState = useCallback((st: NavState) => {
    setNav((old) => {
      // the server re-planned: keep walking along the new line
      if (st.route && old.route && st.route.nodes.join() !== old.route.nodes.join()) sim.current.reroute(st.route.points);
      return st;
    });
    if (st.done) { setRunning(false); }
  }, []);

  const handle = useCallback((r: Partial<NavReply>) => {
    for (const e of r.events ?? []) {
      if (e.text) speak(e.text);
      feel(e.vibrate, settings.haptics);
    }
    if (r.state) applyState(r.state);
  }, [speak, applyState, settings.haptics]);

  const send = useCallback(async (p: Pose, heading?: number | null) => {
    if (busy.current) return;
    busy.current = true;
    pose.current = p;
    try {
      handle(await api.update({ session, x: p.x, y: p.y, floor: p.floor, heading: heading ?? p.heading }));
    } catch (e) {
      setNote(e instanceof ApiError ? e.message : "update failed");
    } finally { busy.current = false; }
  }, [api, session, handle]);

  // band feed for the motor strip (same lines the real band gets)
  useEffect(() => {
    let alive = true, seq = -1;
    const poll = setInterval(async () => {
      try {
        const r = await api.deviceLive(session);
        if (!alive || r.seq === seq) return;
        seq = r.seq;
        const p = decodeLive(r.line);
        if (p) engine.current.onLive(p, Date.now());
      } catch { /* no band feed on this server */ }
    }, 400);
    const tick = setInterval(() => setMotors(engine.current.tick(Date.now())), 40);
    return () => { alive = false; clearInterval(poll); clearInterval(tick); };
  }, [api, session]);

  // a voice or typed reply that started a different trip replaces this screen's whole state
  const onTalk = useCallback(async (r: VoiceReply) => {
    const t = tripFromReply(r);
    if (t && t.session !== session) {
      try {
        const full = await api.update({ session: t.session, x: t.start.x, y: t.start.y, floor: t.start.floor });
        onNewTrip({ api, sid: t.sid, goal: t.goal, profile: trip.profile, session: t.session, state: full.state, start: t.start });
      } catch { setNote("I started the new trip, but couldn't load it."); }
      return;
    }
    if (r.state?.next?.text) setInstr(r.state.next.text);
  }, [api, session, trip.profile, onNewTrip]);

  // simulated walk: 0.5 s ticks at walking speed
  useEffect(() => {
    if (!running || source !== "simulated walk") return;
    const id = setInterval(() => { const p = sim.current.step(0.5); send(p); if (p.done) setRunning(false); }, 500);
    return () => clearInterval(id);
  }, [running, source, send]);

  // steps + compass (experimental)
  useEffect(() => {
    if (!running || source !== "steps + compass") return;
    let stepSub: { remove: () => void } | null = null, magSub: { remove: () => void } | null = null, alive = true;
    (async () => {
      try {
        const ok = (await Pedometer.isAvailableAsync()) && (await Magnetometer.isAvailableAsync());
        if (!ok) { speak("Step counting isn't available here. Use a development build on a phone, or the simulated walk."); setRunning(false); return; }
        await Pedometer.requestPermissionsAsync();
        Magnetometer.setUpdateInterval(200);
        magSub = Magnetometer.addListener(({ x, y }) => { mag.current = { x, y }; });
        stepSub = Pedometer.watchStepCount(({ steps }) => {
          if (!alive) return;
          if (mag.current && offset.current != null) tracker.current.setHeading(modelHeading(offset.current, mag.current.x, mag.current.y));
          if (tracker.current.onSteps(steps) > 0) {
            const t = tracker.current;
            send({ x: t.x, y: t.y, floor: t.floor, heading: t.heading, done: false }, t.heading);
          }
        });
      } catch {
        speak("Couldn't start the step counter."); setRunning(false);
      }
    })();
    return () => { alive = false; stepSub?.remove(); magSub?.remove(); };
  }, [running, source, send, speak]);

  function calibrate() {
    const pts = nav.route?.points ?? [];
    if (!mag.current || pts.length < 2) return speak("Start steps mode first so I can read the compass.");
    const h = Math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]);
    offset.current = calibrateOffset(h, mag.current.x, mag.current.y);
    tracker.current.setHeading(h);
    speak("Calibrated. Walk normally.");
  }

  function nextTurn() {
    const p = sim.current.snapForward();
    send(p);
  }

  async function report(text: string) {
    setProblemOpen(false);
    setThinking(true);
    speak("Got it. Stay where you are while I find another way.");
    try {
      if (text === "I'm lost") {
        try { const r = await api.deviceButton(session, "lost"); return speak(r.say); } catch { /* fall through */ }
      }
      const r: ExceptionReply = await api.exception(session, text);
      (r.say ?? []).forEach((t) => speak(t));
      handle(r);
      setPending(r.pending);
      if (r.pending) speak(`${describePending(r.pending)} Approve it, or reject it to keep going.`);
      setNote(r.mode === "fallback" ? `handled without the agent: ${r.reason ?? ""}` : "");
    } catch (e) {
      speak(e instanceof ApiError && e.status === 404 ? "Problem reports aren't available on this server yet."
        : "I couldn't send that. Stop where you are and ask someone nearby.");
    } finally { setThinking(false); }
  }

  async function decide(d: "approve" | "reject") {
    setPending(null);
    try {
      const r = await api.decide(session, d);
      (r.say ?? []).forEach((t) => speak(t));
      handle(r);
      if (d === "approve" && r.state?.next?.text) speak(r.state.next.text);
    } catch {
      speak("I couldn't reach the server; keeping the current route.");
    }
  }

  function end() { setRunning(false); stopSpeaking(); onEnd(); }

  const bearing = nav.bearing_to_next_deg;
  const dir = direction(bearing);
  return (
    <ScrollView style={{ backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 48 }}>
      <Text accessibilityRole="header" style={{ color: C.dim, fontSize: 18 }}>To the {trip.goal}</Text>
      <View accessibilityLiveRegion="assertive" accessible accessibilityLabel={`Instruction: ${instr}`}>
        <Text style={{ color: C.text, fontSize: 34, fontWeight: "800", lineHeight: 42, marginVertical: 10 }}>{instr}</Text>
      </View>

      <View style={{ flexDirection: "row", alignItems: "center", gap: 18, marginVertical: 8 }}
        accessible accessibilityLabel={nav.done ? "Arrived." : `Next step in ${fmtMetres(nav.distance_to_next_m)}.${dir ? ` ${dir}.` : ""} ${fmtMetres(nav.remaining)} to go.`}>
        {dir && !nav.done && <Text style={{ fontSize: 72, color: C.accent, transform: [{ rotate: `${-(bearing ?? 0)}deg` }] }}>↑</Text>}
        <View>
          <Text style={{ color: C.text, fontSize: 30, fontWeight: "800" }}>{nav.done ? "Arrived" : `${fmtMetres(nav.distance_to_next_m)} to the next step`}</Text>
          {dir && !nav.done && <Text style={{ color: C.dim, fontSize: 18 }}>{dir}</Text>}
          {!nav.done && <Text style={{ color: C.dim, fontSize: 18 }}>{fmtMetres(nav.remaining)} to go{nav.reroutes ? ` · rerouted ${nav.reroutes}×` : ""}</Text>}
        </View>
      </View>

      <TalkBar settings={settings} sid={trip.sid} session={trip.session}
        prompt="Ask anything: what's next, how far, or tell me what's wrong."
        here={() => ({ x: pose.current.x, y: pose.current.y, floor: pose.current.floor })}
        onReply={onTalk} />

      <MotorStrip motors={motors} />

      {nav.done ? (
        <Big kind="primary" label="Arrived. Done" onPress={end} />
      ) : (
        <>
          <Big kind="danger" label={thinking ? "Finding another way…" : "Report a problem"} hint="Elevator out, door locked, lost"
            onPress={() => setProblemOpen(true)} disabled={thinking} />
        </>
      )}

      {source === "simulated walk" && (
        <Big kind={running ? "normal" : "primary"} label={running ? "Pause demo walk" : "Start demo walk"}
          hint="A demo walker follows the route at walking speed" onPress={() => setRunning(!running)} disabled={nav.done} />
      )}
      {source === "steps + compass" && (
        <>
          <P dim>Experimental: step counting drifts a few percent, and compasses are poor near steel.</P>
          <Big kind={running ? "normal" : "primary"} label={running ? "Stop steps" : "Start steps"} onPress={() => setRunning(!running)} />
          <Big label="Calibrate" hint="Face the way the first instruction says, then tap" onPress={calibrate} />
        </>
      )}
      {source === "manual" && <Big kind="primary" label="I'm at the next turn" hint="Moves you to the next point on the route" onPress={nextTurn} />}

      <Big label="End navigation" onPress={end} />
      {!!note && <P dim>{note}</P>}

      <Modal visible={problemOpen} animationType="slide" transparent onRequestClose={() => setProblemOpen(false)}>
        <View style={{ flex: 1, justifyContent: "flex-end", backgroundColor: "#000a" }}>
          <View style={{ backgroundColor: C.bg, padding: 16, borderTopWidth: 2, borderColor: C.line }} accessibilityViewIsModal>
            <H>What's wrong?</H>
            {PROBLEMS.map((p) => <Big key={p} label={p} onPress={() => report(p)} />)}
            <Field label="Something else" value={freeText} onChangeText={setFreeText} placeholder="the hallway is closed"
              returnKeyType="send" onSubmitEditing={() => freeText.trim() && report(freeText.trim())} />
            <Big label="Send" onPress={() => freeText.trim() && report(freeText.trim())} disabled={!freeText.trim()} />
            <Big label="Cancel" onPress={() => setProblemOpen(false)} />
          </View>
        </View>
      </Modal>

      <Modal visible={!!pending} animationType="slide" transparent onRequestClose={() => decide("reject")}>
        <View style={{ flex: 1, justifyContent: "flex-end", backgroundColor: "#000a" }}>
          <View style={{ backgroundColor: C.bg, padding: 16, borderTopWidth: 2, borderColor: C.accent }} accessibilityViewIsModal>
            <H>New route?</H>
            <P>{pending ? describePending(pending) : ""}</P>
            <Big kind="primary" label="Approve new route" onPress={() => decide("approve")} />
            <Big label="Reject, keep current route" onPress={() => decide("reject")} />
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
}
