// nudge mobile: indoor guidance for blind and low-vision travellers. four screens behind a bottom tab bar.
import { StatusBar } from "expo-status-bar";
import React, { useEffect, useState } from "react";
import { Pressable, SafeAreaView, Text, View } from "react-native";
import { Band } from "./src/screens/Band";
import { Navigate } from "./src/screens/Navigate";
import { SettingsScreen } from "./src/screens/SettingsScreen";
import { WhereTo } from "./src/screens/WhereTo";
import { DEFAULTS, loadSettings, saveSettings, type Settings, type Trip } from "./src/state";
import { C } from "./src/ui/kit";

const TABS = ["Where to", "Navigate", "Band", "Settings"] as const;
type Tab = (typeof TABS)[number];

// hidden panes are removed from layout and from the screen reader, but keep their state and timers
function Pane({ visible, children }: { visible: boolean; children: React.ReactNode }) {
  return (
    <View style={{ flex: 1, display: visible ? "flex" : "none" }} importantForAccessibility={visible ? "auto" : "no-hide-descendants"}
      accessibilityElementsHidden={!visible}>
      {children}
    </View>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("Where to");
  const [settings, setSettingsState] = useState<Settings>(DEFAULTS);
  const [trip, setTrip] = useState<Trip | null>(null);

  useEffect(() => { loadSettings().then(setSettingsState); }, []);
  const setSettings = (s: Settings) => { setSettingsState(s); saveSettings(s); };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar style="light" />
      <View style={{ flex: 1 }}>
        {tab === "Where to" && <WhereTo settings={settings} setSettings={setSettings}
          onStarted={(t) => { setTrip(t); setTab("Navigate"); }} />}
        {/* navigate and band stay mounted while a trip runs, so the walk and the band link keep going on other tabs */}
        <Pane visible={tab === "Navigate"}>
          {trip
            ? <Navigate key={trip.session} trip={trip} settings={settings} onEnd={() => { setTrip(null); setTab("Where to"); }} />
            : <View style={{ padding: 16 }}><Text style={{ color: C.text, fontSize: 22 }}>No trip yet. Choose a destination under Where to.</Text></View>}
        </Pane>
        <Pane visible={tab === "Band"}><Band trip={trip} settings={settings} /></Pane>
        {tab === "Settings" && <SettingsScreen settings={settings} setSettings={setSettings} />}
      </View>
      <View accessibilityRole="tablist" style={{ flexDirection: "row", borderTopWidth: 2, borderColor: C.line }}>
        {TABS.map((t) => (
          <Pressable key={t} accessibilityRole="tab" accessibilityLabel={t} accessibilityState={{ selected: t === tab }}
            onPress={() => setTab(t)}
            style={{ flex: 1, minHeight: 64, alignItems: "center", justifyContent: "center",
              backgroundColor: t === tab ? C.accent : C.bg }}>
            <Text style={{ fontSize: 16, fontWeight: "800", color: t === tab ? C.accentText : C.text }}>
              {t === tab ? `• ${t}` : t}
            </Text>
          </Pressable>
        ))}
      </View>
    </SafeAreaView>
  );
}
