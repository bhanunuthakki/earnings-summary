/**
 * MultiSelect preview — the kit's multi-choice picker (replaces the native
 * `<select multiple>`). The trigger shows the selection as `<Chip>`s and opens
 * the shared `.k-menu` popover of kit-checkbox rows. Shown in its resting
 * (closed) state, chips summarizing the current selection.
 */
import * as React from "react";
import { Label, MultiSelect } from "@earnings-summary/design-system";

const TICKERS = [
  { value: "NU", label: "NU — Nu Holdings" },
  { value: "MELI", label: "MELI — MercadoLibre" },
  { value: "NOW", label: "NOW — ServiceNow" },
  { value: "WIX", label: "WIX — Wix.com" },
  { value: "RBRK", label: "RBRK — Rubrik" },
];

export function CompareTickers() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 260 }}>
      <Label>Compare tickers</Label>
      <MultiSelect options={TICKERS} defaultValue={["NU", "MELI", "RBRK"]} />
    </div>
  );
}

export function Empty() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 260 }}>
      <Label>Peer set</Label>
      <MultiSelect options={TICKERS} placeholder="Add peers…" />
    </div>
  );
}
