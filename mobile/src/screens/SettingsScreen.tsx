import React, { useState } from "react";
import { ScrollView, Switch, Text, View } from "react-native";
import { Api } from "../lib/api";
import { stepLength } from "../lib/live";
import { say } from "../lib/voice";
import { POS_SOURCES, type Settings } from "../state";
import { Big, C, Choice, Field, H, P } from "../ui/kit";

export function SettingsScreen({ settings, setSettings }: { settings: Settings; setSettings: (s: Settings) => void }) {
  const [server, setServer] = useState(settings.server);
  const [msg, setMsg] = useState("");
  const [dev, setDev] = useState(false);
  const [tenSteps, setTenSteps] = useState("");
  const rate = (d: number) => {
    const r = Math.max(0.5, Math.min(2, Math.round((settings.rate + d) * 10) / 10));
    setSettings({ ...settings, rate: r });
    say(`Speech rate ${r}`, r);
  };

  async function test() {
    setSettings({ ...settings, server });
    let m: string;
    try { await new Api(server, 5000).profiles(); m = "Connected to the Pi."; }
    catch { m = `Can't reach ${server}. Put the phone and the Pi on the same hotspot, then check the address.`; }
    setMsg(m);
    say(m, settings.rate);
  }

  const code = settings.caretakerCode;
  const line = settings.caretakerLine ?? "your Nudge iMessage line";
  const spoken = code ? code.split("").join(" ") : "";

  return (
    <ScrollView style={{ backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 48 }}>
      <H>Settings</H>

      <H>Caretaker</H>
      <View accessible accessibilityLabel={code
        ? `Your caretaker code is ${spoken}. Your caretaker texts follow ${spoken} to ${line}. They then get a message when you start, change floors, arrive, or ask for help.`
        : "No caretaker code yet. Tap Get a caretaker code."}>
        {code ? (
          <>
            <Text style={{ color: C.text, fontSize: 40, fontWeight: "800", letterSpacing: 4 }}>{code}</Text>
            <P>Your caretaker texts <Text style={{ fontWeight: "800" }}>follow {code}</Text> to {line}.</P>
            <P dim>They get a message when you start, change floors, arrive, or ask for help.</P>
          </>
        ) : <P>No caretaker code yet.</P>}
      </View>
      <Big label={code ? "Read my code aloud" : "Get a caretaker code"} hint="Your caretaker uses it to follow your trips"
        onPress={async () => {
          if (code) return say(`Your code is ${spoken}. Your caretaker texts follow ${spoken} to ${line}.`, settings.rate);
          try {
            const r = await new Api(settings.server, 5000).registerTraveller(settings.traveller);
            if (r.code) { setSettings({ ...settings, caretakerCode: r.code }); say(`Your code is ${r.code.split("").join(" ")}`, settings.rate); }
            else say("The server didn't return a code.", settings.rate);
          } catch { say("I couldn't reach the server.", settings.rate); }
        }} />

      <H>Connect to Pi</H>
      <Field label="Pi address" value={server} onChangeText={setServer} keyboardType="url" autoCapitalize="none" autoCorrect={false}
        placeholder="http://172.20.10.2:8765" accessibilityHint="The Raspberry Pi running Nudge, with port 8765" />
      <P dim>On iPhone hotspot the Pi is usually 172.20.10.2–14, port 8765.</P>
      <Big kind="primary" label="Save and test connection" onPress={test} />
      {!!msg && <View accessibilityLiveRegion="polite"><P>{msg}</P></View>}

      <H>Step length</H>
      <P>{settings.stepM} meters per step</P>
      <P dim>To measure it: walk 10 normal steps, then type how many meters that was.</P>
      <Field label="Meters for 10 steps" value={tenSteps} onChangeText={setTenSteps} keyboardType="decimal-pad" placeholder="7" />
      <Big label="Set step length" disabled={!tenSteps.trim()} onPress={() => {
        const m = stepLength(parseFloat(tenSteps.replace(",", ".")));
        if (m == null) return say("That doesn't look right. Ten steps is usually 5 to 9 meters.", settings.rate);
        setSettings({ ...settings, stepM: m });
        setTenSteps("");
        say(`Step length set to ${m} meters.`, settings.rate);
      }} />

      <H>Speech</H>
      <P>Rate: {settings.rate}</P>
      <View style={{ flexDirection: "row", gap: 12 }}>
        <View style={{ flex: 1 }}><Big label="Slower" onPress={() => rate(-0.1)} /></View>
        <View style={{ flex: 1 }}><Big label="Faster" onPress={() => rate(0.1)} /></View>
      </View>

      <H>Haptics</H>
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", minHeight: 64 }}>
        <Text style={{ color: C.text, fontSize: 20 }} nativeID="hap">Phone vibration</Text>
        <Switch accessibilityLabel="Phone vibration" accessibilityLabelledBy="hap" value={settings.haptics}
          onValueChange={(v) => setSettings({ ...settings, haptics: v })} />
      </View>

      <P dim>Every distance is in meters.</P>

      {/* demo plumbing, out of the traveller's way */}
      <Big label={dev ? "Hide developer settings" : "Developer settings"} onPress={() => setDev(!dev)}
        hint="Where positions come from" />
      {dev && (
        <View>
          <Choice label="Where positions come from" options={POS_SOURCES} value={settings.posSource}
            onChange={(v) => setSettings({ ...settings, posSource: v })}
            describe={(v) => v === "live" ? "live: this phone's steps and compass" : v} />
        </View>
      )}
    </ScrollView>
  );
}
