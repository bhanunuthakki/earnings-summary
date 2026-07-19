/**
 * Select preview — the kit's single-choice dropdown. The default is the popover
 * composite (`.k-trigger` button + `.k-menu` popover, full keyboard nav); the
 * `native` escape hatch renders a plain styled `<select>`. Shown here in their
 * resting (closed) state — the popover surface itself is in the Menu preview.
 */
import * as React from "react";
import { Label, Select } from "@earnings-summary/design-system";

const PERIODS = [
  { value: "Q1-2026" },
  { value: "Q2-2026" },
  { value: "FY-2025" },
  { value: "FY-2024", label: "FY-2024 (restated)" },
];

export function PeriodSingle() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>Period</Label>
      <Select options={PERIODS} defaultValue="Q2-2026" />
    </div>
  );
}

export function Placeholder() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>Valuation basis</Label>
      <Select
        options={[
          { value: "dcf", label: "Reverse DCF" },
          { value: "multiples", label: "Peer multiples" },
          { value: "sotp", label: "Sum-of-the-parts" },
        ]}
        placeholder="Choose a basis…"
      />
    </div>
  );
}

export function NativeEscapeHatch() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>Period (native)</Label>
      <Select options={PERIODS} defaultValue="Q2-2026" native style={{ width: "100%" }} />
    </div>
  );
}
