// speaking and feeling. platform glue around the pure modules; not unit tested.
import * as Haptics from "expo-haptics";
import * as Speech from "expo-speech";
import { AccessibilityInfo, Platform, Vibration } from "react-native";
import { androidPattern, pulseSchedule } from "./haptics";

let screenReader = false;
AccessibilityInfo.isScreenReaderEnabled().then((v) => { screenReader = v; }).catch(() => {});
AccessibilityInfo.addEventListener?.("screenReaderChanged", (v: boolean) => { screenReader = v; });

// with voiceover/talkback on, let it speak (one voice, user's rate); otherwise use our own tts
export function say(text: string, rate = 1.0, interrupt = true) {
  if (!text) return;
  if (screenReader) {
    AccessibilityInfo.announceForAccessibility(text);
    return;
  }
  if (interrupt) Speech.stop().catch(() => {});
  Speech.speak(text, { rate, language: "en-US" });
}

export function stopSpeaking() {
  Speech.stop().catch(() => {});
}

const IMPACT: Record<string, Haptics.ImpactFeedbackStyle> = {
  light: Haptics.ImpactFeedbackStyle.Light,
  medium: Haptics.ImpactFeedbackStyle.Medium,
  heavy: Haptics.ImpactFeedbackStyle.Heavy,
};

// play a server pattern like [120, 80, 120]
export function feel(pattern: number[] | null | undefined, enabled = true) {
  if (!enabled || !pattern || !pattern.length) return;
  if (Platform.OS === "android") {
    Vibration.vibrate(androidPattern(pattern));
  } else if (Platform.OS === "ios") {
    for (const p of pulseSchedule(pattern)) setTimeout(() => { Haptics.impactAsync(IMPACT[p.strength]).catch(() => {}); }, p.at);
  } else if (typeof navigator !== "undefined" && "vibrate" in navigator) {
    try { (navigator as Navigator).vibrate(pattern); } catch { /* blocked until a tap */ }
  }
}
