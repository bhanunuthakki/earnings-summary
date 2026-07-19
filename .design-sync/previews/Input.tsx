/**
 * Input preview — the bare-`<input>` form-control baseline from controls.css
 * (no class needed). Text/search/number/date variants plus a Label-captioned
 * composition, all in the platform's research vocabulary.
 */
import * as React from "react";
import { Input, Label } from "@earnings-summary/design-system";

export function TextValue() {
  return (
    <div style={{ maxWidth: 280 }}>
      <Input type="text" defaultValue="NU credit quality — NPL 15-90 watch" />
    </div>
  );
}

export function SearchPlaceholder() {
  return (
    <div style={{ maxWidth: 280 }}>
      <Input type="search" placeholder="Ask about NU's credit quality…" />
    </div>
  );
}

export function NumberAndDate() {
  return (
    <div style={{ display: "flex", gap: 8, maxWidth: 320 }}>
      <Input type="number" defaultValue="118.40" step="0.01" style={{ width: 110 }} />
      <Input type="date" defaultValue="2026-07-18" style={{ width: 160 }} />
    </div>
  );
}

export function WithLabelCaption() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 220 }}>
      <Label>Ticker</Label>
      <Input type="text" defaultValue="MELI" autoFocus />
    </div>
  );
}
