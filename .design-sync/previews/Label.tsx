/**
 * Label preview — `.k-label`, the uppercase field/section caption, composed
 * the way form fields / KPI table headers actually sit: caption over value.
 */
import * as React from "react";
import { Label, NumText, Pill, TickerLabel } from "@earnings-summary/design-system";

export function FieldCaptions() {
  return (
    <div style={{ display: "flex", gap: 24 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <Label>Next ER</Label>
        <span>Aug 12, 2026</span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <Label>vs DCF FV</Label>
        <NumText value={-18.5} format={(n) => `${n.toFixed(1)}%`} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <Label>Thesis</Label>
        <span>
          <Pill tone="ok">INTACT</Pill>
        </span>
      </div>
    </div>
  );
}

export function SectionCaption() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <Label>Holdings under review</Label>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <TickerLabel ticker="NU" name="Nu Holdings" />
        <TickerLabel ticker="MELI" name="MercadoLibre" />
        <TickerLabel ticker="BKNG" name="Booking Holdings" />
      </div>
    </div>
  );
}

export function WithExtraClass() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <Label className="demo-extra">Rate-limit budget</Label>
      <span>240 calls / day (FMP stable)</span>
    </div>
  );
}
