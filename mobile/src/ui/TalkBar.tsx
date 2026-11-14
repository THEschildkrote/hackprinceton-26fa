// talk to nudge: one big button (tap to talk, tap to send), with typed text and quick phrases when the
// phone can't do speech recognition (expo go). replies are spoken through voiceover/talkback when it's on.
import React, { useRef, useState } from "react";
import { View } from "react-native";
import { Api, ApiError } from "../lib/api";
import { listen, QUICK_PHRASES, speechAvailable, type VoiceReply } from "../lib/talk";
import { say, stopSpeaking } from "../lib/voice";
import type { Settings } from "../state";
import { Big, C, Field, P } from "./kit";

export function TalkBar({ settings, sid, session, here, onReply, prompt }: {
  settings: Settings;
  sid?: string | null;
  session?: string | null;
  here?: () => { x: number; y: number; floor: string } | null;
  onReply?: (r: VoiceReply) => void;
  prompt?: string;
}) {
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState("");
  const [last, setLast] = useState(prompt ?? "Say where you want to go, like: take me to the kitchen.");
  const stopRef = useRef<(() => void) | null>(null);
  const canListen = speechAvailable();

  async function send(text: string) {
    text = text.trim();
    if (!text || !settings.traveller) return;
    setBusy(true);
    try {
      const pos = here?.() ?? null;
      const r = await new Api(settings.server).voiceTurn({
        traveller: settings.traveller, text, sid: sid ?? null, session: session ?? null, ...(pos ?? {}),
      });
      setLast(r.say);
      say(r.say, settings.rate);
      onReply?.(r);
    } catch (e) {
      const m = e instanceof ApiError && e.status === 404
        ? "This server has no voice service yet."
        : e instanceof ApiError ? `I couldn't reach Nudge: ${e.message}` : "I couldn't reach Nudge.";
      setLast(m);
      say(m, settings.rate);
    } finally { setBusy(false); }
  }

  async function toggleTalk() {
    if (listening) { stopRef.current?.(); return; }
    stopSpeaking();
    const l = listen();
    stopRef.current = l.stop;
    setListening(true);
    const heard = await l.done;
    setListening(false);
    stopRef.current = null;
    if (heard) send(heard);
    else { setLast("I didn't hear anything. Tap Talk and speak, then tap again."); }
  }

  return (
    <View style={{ gap: 8, marginBottom: 12 }}>
      {canListen ? (
        <Big kind={listening ? "danger" : "primary"}
          label={listening ? "Listening. Tap to send" : busy ? "Thinking…" : "Talk"}
          hint="Tap, speak, then tap again to send what you said to Nudge"
          onPress={toggleTalk} disabled={busy && !listening} />
      ) : (
        <P dim>Voice input needs the Nudge app build. In Expo Go, type below or use a quick phrase.</P>
      )}
      <Field label="Say it by typing" value={typed} onChangeText={setTyped} returnKeyType="send"
        placeholder="take me to the kitchen" accessibilityHint="Type what you would say, then press send"
        onSubmitEditing={() => { send(typed); setTyped(""); }} />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {QUICK_PHRASES.map((p) => (
          <View key={p} style={{ minWidth: "47%", flexGrow: 1 }}>
            <Big label={p} onPress={() => send(p)} disabled={busy} hint={`Says "${p}" to Nudge`} />
          </View>
        ))}
      </View>
      <View accessibilityLiveRegion="polite" style={{ backgroundColor: C.card, borderRadius: 12, padding: 12 }}>
        <P>{last}</P>
      </View>
    </View>
  );
}
