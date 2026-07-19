/**
 * NumText preview — `.k-num-pos` / `.k-num-neg`, the one green/red number
 * text (P&L cells, KPI deltas, alpha). Kit convention: zero renders pos —
 * `v >= 0 ? pos : neg`, never a third neutral tone.
 */
import * as React from "react";
import { NumText, Label } from "@earnings-summary/design-system";

export function DeltaColumn() {
  const rows: Array<[string, number, string]> = [
    ["NU revenue YoY", 49.1, "%"],
    ["MELI GMV YoY", 18.4, "%"],
    ["WIX bookings YoY", -4.0, "pp"],
    ["RBRK NRR", -2.0, "pp"],
    ["META capex YoY", -38.5, "%"],
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.map(([label, v, unit]) => (
        <div key={label} style={{ display: "flex", gap: 12, justifyContent: "space-between", width: 260 }}>
          <span>{label}</span>
          <NumText value={v} format={(n) => `${n > 0 ? "+" : ""}${n.toFixed(1)}${unit}`} />
        </div>
      ))}
    </div>
  );
}

export function ZeroIsPos() {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
      <Label>QoQ change</Label>
      <NumText value={0} format={(n) => `${n.toFixed(1)}%`} />
      <span style={{ fontSize: 12 }}>(zero renders pos — kit convention)</span>
    </div>
  );
}

export function CustomFormat() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", gap: 12 }}>
        <Label>Unrealized P&amp;L</Label>
        <NumText value={12480.5} format={(n) => `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} />
      </div>
      <div style={{ display: "flex", gap: 12 }}>
        <Label>vs DCF FV</Label>
        <NumText value={-0.185} format={(n) => `${(n * 100).toFixed(1)}%`} />
      </div>
    </div>
  );
}
