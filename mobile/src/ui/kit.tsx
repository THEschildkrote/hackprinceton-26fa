// shared look: dark, high contrast, big targets (>= 56 dp), every control labelled for screen readers.
import React from "react";
import { Pressable, StyleSheet, Text, TextInput, View, type TextInputProps } from "react-native";

export const C = {
  bg: "#0b0d10",
  card: "#161a20",
  line: "#2b3240",
  text: "#f5f7fa",
  dim: "#b6bfcc",
  accent: "#ffd23f", // yellow on near-black: ~14:1 contrast
  accentText: "#111111",
  danger: "#ff6b6b",
  ok: "#5be49b",
};

export const MIN_TARGET = 64;

export function Big({ label, hint, onPress, kind = "normal", disabled, selected }: {
  label: string; hint?: string; onPress: () => void; kind?: "primary" | "normal" | "danger"; disabled?: boolean; selected?: boolean;
}) {
  const bg = kind === "primary" ? C.accent : kind === "danger" ? C.danger : C.card;
  const fg = kind === "primary" || kind === "danger" ? C.accentText : C.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={hint}
      accessibilityState={{ disabled: !!disabled, selected: !!selected }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [s.btn, { backgroundColor: bg, opacity: disabled ? 0.45 : pressed ? 0.75 : 1 },
        selected && s.selected]}
    >
      {/* selection is also spelled out, never colour alone */}
      <Text style={[s.btnText, { color: fg }]}>{selected ? `✓ ${label}` : label}</Text>
    </Pressable>
  );
}

export function Choice<T extends string>({ label, options, value, onChange, describe }: {
  label: string; options: readonly T[]; value: T; onChange: (v: T) => void; describe?: (v: T) => string;
}) {
  return (
    <View accessibilityRole="radiogroup" accessibilityLabel={label} style={s.group}>
      <H>{label}</H>
      <View style={s.row}>
        {options.map((o) => (
          <Pressable
            key={o}
            accessibilityRole="radio"
            accessibilityLabel={describe ? describe(o) : o}
            accessibilityState={{ checked: o === value }}
            onPress={() => onChange(o)}
            style={[s.chip, o === value && s.chipOn]}
          >
            <Text style={[s.chipText, o === value && { color: C.accentText }]}>{o === value ? `✓ ${o}` : o}</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

export function H({ children }: { children: React.ReactNode }) {
  return <Text accessibilityRole="header" style={s.h}>{children}</Text>;
}

export function P({ children, dim }: { children: React.ReactNode; dim?: boolean }) {
  return <Text style={[s.p, dim && { color: C.dim }]}>{children}</Text>;
}

export function Field({ label, ...rest }: TextInputProps & { label: string }) {
  return (
    <View style={s.group}>
      <Text style={s.label} nativeID={`lbl-${label}`}>{label}</Text>
      <TextInput
        accessibilityLabel={label}
        accessibilityLabelledBy={`lbl-${label}`}
        placeholderTextColor={C.dim}
        style={s.input}
        autoCapitalize="none"
        autoCorrect={false}
        {...rest}
      />
    </View>
  );
}

export const s = StyleSheet.create({
  btn: { minHeight: MIN_TARGET, borderRadius: 14, paddingHorizontal: 18, justifyContent: "center", marginVertical: 6,
    borderWidth: 2, borderColor: C.line },
  selected: { borderColor: C.accent, borderWidth: 3 },
  btnText: { fontSize: 22, fontWeight: "700", textAlign: "center" },
  group: { marginVertical: 8 },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { minHeight: MIN_TARGET, minWidth: 96, paddingHorizontal: 16, borderRadius: 12, justifyContent: "center",
    backgroundColor: C.card, borderWidth: 2, borderColor: C.line },
  chipOn: { backgroundColor: C.accent, borderColor: C.accent },
  chipText: { fontSize: 20, fontWeight: "700", color: C.text, textAlign: "center" },
  h: { fontSize: 26, fontWeight: "800", color: C.text, marginTop: 12, marginBottom: 6 },
  p: { fontSize: 19, color: C.text, lineHeight: 27, marginVertical: 4 },
  label: { fontSize: 18, color: C.dim, marginBottom: 6 },
  input: { minHeight: MIN_TARGET, borderRadius: 12, borderWidth: 2, borderColor: C.line, backgroundColor: C.card,
    color: C.text, fontSize: 22, paddingHorizontal: 14 },
});
