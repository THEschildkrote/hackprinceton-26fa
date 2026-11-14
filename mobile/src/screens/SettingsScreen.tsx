import React, { useState } from "react";
import { ScrollView, Switch, Text, View } from "react-native";
import { Api } from "../lib/api";
import { say } from "../lib/voice";
import type { Settings } from "../state";
import { Big, C, Field, H, P } from "../ui/kit";

export function SettingsScreen({ settings, setSettings }: { settings: Settings; setSettings: (s: Settings) => void }) {
  const [server, setServer] = useState(settings.server);
  const [msg, setMsg] = useState("");
  const rate = (d: number) => {
    const r = Math.max(0.5, Math.min(2, Math.round((settings.rate + d) * 10) / 10));
    setSettings({ ...settings, rate: r });
    say(`Speech rate ${r}`, r);
  };

  async function test() {
    setSettings({ ...settings, server });
    try { await new Api(server, 5000).profiles(); setMsg("Server reachable."); }
    catch { setMsg(`Can't reach ${server}. Phone and laptop must be on the same wifi.`); }
    say(msg, settings.rate);
  }

  return (
    <ScrollView style={{ backgroundColor: C.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 48 }}>
      <H>Settings</H>
      <Field label="Server address" value={server} onChangeText={setServer} keyboardType="url"
        placeholder="http://192.168.1.10:8765" accessibilityHint="The laptop running the Nudge server" />
      <Big label="Save and test server" onPress={test} />
      {!!msg && <View accessibilityLiveRegion="polite"><P>{msg}</P></View>}

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

      <H>Units</H>
      <P>Meters. Every distance is in meters.</P>
    </ScrollView>
  );
}
