// live mode: this phone's steps and compass stream to the nudge server on the pi, which moves the traveller along
// the map and drives the collar. the phone only senses and speaks; all geometry lives on the pi.
import * as Location from "expo-location";
import { Accelerometer } from "expo-sensors";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Platform, Text, View } from "react-native";
import { ApiError, type StepReply } from "../lib/api";
import { angleDiff, facingHint, HeadingFilter, norm, pickHeading, poorAccuracy } from "../lib/heading";
import { StepQueue } from "../lib/live";
import { StepDetector, type Sample } from "../lib/steps";
import { feel } from "../lib/voice";
import type { Settings, Trip } from "../state";
import { Big, C, P } from "./kit";

type Phase = "calibrate" | "walking" | "done";
type Link = "waiting" | "ok" | "down" | "unsupported" | "restarted";

export function LivePanel({ trip, settings, fake, onReply, speak, lastInstruction }: {
  trip: Trip; settings: Settings; fake: boolean;
  onReply: (r: StepReply) => void; speak: (t: string) => void; lastInstruction: () => string;
}) {
  const { api, session } = trip;
  const [phase, setPhase] = useState<Phase>("calibrate");
  const [heading, setHeading] = useState<number | null>(null);
  const [accuracy, setAccuracy] = useState<number | null>(null);
  const [steps, setSteps] = useState(0);
  const [link, setLink] = useState<Link>("waiting");
  const [sensorNote, setSensorNote] = useState("");

  const phaseRef = useRef<Phase>("calibrate");
  const headingRef = useRef<number | null>(null);
  const filter = useRef(new HeadingFilter(0.3));
  const detector = useRef(new StepDetector());
  const queue = useRef(new StepQueue());
  const sending = useRef(false);
  const lastSent = useRef(0);
  const linkRef = useRef<Link>("waiting");
  const facedRight = useRef(false);
  const bearing = useRef<number | null>(null);  // last steer from the pi, + = left (fake walk follows it)

  const setLinkBoth = (l: Link) => { linkRef.current = l; setLink(l); };
  const go = (p: Phase) => { phaseRef.current = p; setPhase(p); };

  const onHeading = useCallback((deg: number) => {
    const h = filter.current.push(deg);
    headingRef.current = h;
    setHeading(Math.round(h));
    // before calibrating: tell them once when they face the first instruction's direction
    if (phaseRef.current === "calibrate" && trip.startHeading != null) {
      const ok = Math.abs(angleDiff(h, trip.startHeading)) <= 20;
      if (ok && !facedRight.current) { facedRight.current = true; speak("You're facing the right way. Tap Calibrate."); feel([60], settings.haptics); }
      if (!ok && Math.abs(angleDiff(h, trip.startHeading)) > 35) facedRight.current = false;
    }
  }, [trip.startHeading, speak, settings.haptics]);

  // send everything pending (or a heading-only update); failed sends keep their steps for the next try
  const flush = useCallback(async () => {
    if (sending.current || phaseRef.current !== "walking") return;
    sending.current = true;
    const n = queue.current.take();
    try {
      const r = await api.step({ session, steps: n, heading_deg: headingRef.current, step_m: settings.stepM });
      queue.current.ack();
      lastSent.current = Date.now();
      if (linkRef.current === "down") speak("Back in touch with the Pi.");
      setLinkBoth("ok");
      bearing.current = r.bearing_to_next_deg ?? r.state?.bearing_to_next_deg ?? null;
      onReply(r);
      if (r.done || r.state?.done) go("done");
    } catch (e) {
      queue.current.fail();
      lastSent.current = Date.now();  // retry on the next tick, not in a tight loop
      if (e instanceof ApiError && e.status === 404 && /session/i.test(e.message)) {
        // the pi restarted and forgot this trip: steps can't be replayed into a new one
        speak("The Pi restarted, so this trip ended. Stop where you are, then start the trip again.");
        setLinkBoth("restarted");
        go("done");
      } else if (e instanceof ApiError && e.status === 404) {
        if (linkRef.current !== "unsupported") speak("This Pi doesn't support live steps yet. Update the Nudge server on it.");
        setLinkBoth("unsupported");
      } else {
        if (linkRef.current !== "down") {
          const last = lastInstruction();
          speak(`I lost the Pi. Slow down and keep going carefully.${last ? ` Last instruction: ${last}` : ""}`);
        }
        setLinkBoth("down");
      }
    } finally { sending.current = false; }
  }, [api, session, settings.stepM, onReply, speak, lastInstruction]);

  const onStep = useCallback(() => {
    if (phaseRef.current !== "walking") return;  // steps while standing at the start don't count
    queue.current.add();
    setSteps(queue.current.total);
    flush();
  }, [flush]);

  // heading-only updates when standing still, and retries while the pi is unreachable
  useEffect(() => {
    const id = setInterval(() => {
      if (phaseRef.current === "walking" && Date.now() - lastSent.current > 900) flush();
    }, 1000);
    return () => clearInterval(id);
  }, [flush]);

  // real sensors: compass via ios heading (tilt-compensated, true north when available) + accelerometer steps
  useEffect(() => {
    if (fake) return;
    let headSub: { remove: () => void } | null = null, accSub: { remove: () => void } | null = null, alive = true;
    (async () => {
      try {
        const perm = await Location.requestForegroundPermissionsAsync();
        if (perm.status !== "granted") setSensorNote("Allow location so I can read the compass. Settings, Nudge, Location.");
        else if (Platform.OS !== "web") {
          headSub = await Location.watchHeadingAsync((h) => {
            if (!alive) return;
            setAccuracy(h.accuracy);
            onHeading(pickHeading(h));
          });
        }
      } catch { setSensorNote("The compass isn't available on this device."); }
      try {
        if (!(await Accelerometer.isAvailableAsync())) { setSensorNote("No accelerometer here, so I can't count steps."); return; }
        Accelerometer.setUpdateInterval(20);  // 50 hz
        accSub = Accelerometer.addListener(({ x, y, z }) => {
          if (alive && detector.current.push({ t: Date.now(), x, y, z })) onStep();
        });
      } catch { setSensorNote("Couldn't start step counting."); }
    })();
    return () => { alive = false; headSub?.remove(); accSub?.remove(); };
  }, [fake, onHeading, onStep]);

  // dev-only fake walk: synthetic samples through the real detector; it steers by the pi's own guidance
  useEffect(() => {
    if (!fake) return;
    let h = trip.startHeading ?? 0, t = 0, seed = 11;
    const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648 - 0.5; };
    const id = setInterval(() => {
      t += 20;
      const walking = phaseRef.current === "walking";
      const ph = 2 * Math.PI * 1.8 * (t / 1000);
      const bounce = walking ? 0.25 * Math.sin(ph) + 0.075 * Math.sin(2 * ph + 0.6) : 0;
      const s: Sample = { t, x: 0.02 * rnd(), y: 0.02 * rnd(), z: 1 + bounce + 0.02 * rnd() };
      if (detector.current.push(s)) onStep();
      if (t % 200 === 0) {
        // turn toward the pi's steer a little each tick (bearing + = left, compass is clockwise)
        if (walking && bearing.current != null) h = norm(h - Math.max(-25, Math.min(25, bearing.current * 0.4)));
        onHeading(norm(h + 3 * rnd()));
        setAccuracy(3);
      }
    }, 20);
    return () => clearInterval(id);
  }, [fake, trip.startHeading, onHeading, onStep]);

  async function calibrate() {
    const h = headingRef.current;
    if (h == null) return speak("I can't read the compass yet. Hold the phone flat in front of you and try again.");
    try {
      const r = await api.calibrate(session, Math.round(h));
      setLinkBoth("ok");
      onReply(r);
      go("walking");
      lastSent.current = Date.now();
      if (!r.say?.length) speak("Calibrated. Start walking.");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        // older pi server: walk anyway, steps still stream
        setLinkBoth("unsupported");
        speak("This Pi can't calibrate yet. I'll try to guide you anyway.");
        go("walking");
      } else {
        setLinkBoth("down");
        speak("I can't reach the Pi. Check that the phone and the Pi are on the same hotspot.");
      }
    }
  }

  const poor = accuracy != null && poorAccuracy(accuracy);
  const linkText = { waiting: "Pi: waiting", ok: "Pi: connected", down: "Pi not reachable", unsupported: "Pi: update needed",
    restarted: "Pi restarted" }[link];
  const hint = phase === "calibrate" && heading != null && trip.startHeading != null ? facingHint(heading, trip.startHeading) : null;

  return (
    <View>
      {link === "restarted" && (
        <View accessibilityLiveRegion="assertive" accessible accessibilityLabel="The Pi restarted, so this trip ended. Start the trip again."
          style={{ backgroundColor: "#5c1414", borderColor: C.danger, borderWidth: 2, borderRadius: 12, padding: 12, marginVertical: 6 }}>
          <Text style={{ color: "#fff", fontSize: 22, fontWeight: "800" }}>Trip ended</Text>
          <Text style={{ color: "#fff", fontSize: 16 }}>The Pi restarted and lost this trip. Go back to Where to and start again.</Text>
        </View>
      )}
      {link === "down" && (
        <View accessibilityLiveRegion="assertive" accessible accessibilityLabel="Pi not reachable. Your steps are saved and will be sent when it's back."
          style={{ backgroundColor: "#5c1414", borderColor: C.danger, borderWidth: 2, borderRadius: 12, padding: 12, marginVertical: 6 }}>
          <Text style={{ color: "#fff", fontSize: 22, fontWeight: "800" }}>Pi not reachable</Text>
          <Text style={{ color: "#fff", fontSize: 16 }}>Steps are saved and resent when it's back ({queue.current.pending + queue.current.inflight} waiting).</Text>
        </View>
      )}

      {phase === "calibrate" && (
        <>
          {!!hint && <Text accessibilityLiveRegion="polite" style={{ color: C.text, fontSize: 24, fontWeight: "700", marginVertical: 6 }}>{hint}</Text>}
          <Big kind="primary" label="Calibrate" hint="Face the way the first instruction says, hold the phone in front of you, then tap"
            onPress={calibrate} />
        </>
      )}
      {phase === "walking" && <P dim>Walk normally. I'll tell you before each turn.</P>}

      <View accessible accessibilityLabel={`${steps} steps. ${heading == null ? "No compass yet" : `Facing ${heading} degrees`}. ${linkText}.`}
        style={{ flexDirection: "row", flexWrap: "wrap", gap: 14, marginVertical: 6 }}>
        <Text style={{ color: C.dim, fontSize: 16 }}>{steps} steps</Text>
        <Text style={{ color: C.dim, fontSize: 16 }}>heading {heading == null ? "-" : `${heading}°`}</Text>
        <Text style={{ color: link === "ok" ? C.ok : link === "down" ? C.danger : C.dim, fontSize: 16, fontWeight: "700" }}>{linkText}</Text>
        {fake && <Text style={{ color: C.dim, fontSize: 16 }}>fake walk</Text>}
      </View>
      {poor && <P dim>Compass interference: move away from metal, or wave the phone in a figure 8.</P>}
      {!!sensorNote && <P>{sensorNote}</P>}
    </View>
  );
}
