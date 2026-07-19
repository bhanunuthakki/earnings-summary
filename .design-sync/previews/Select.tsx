/**
 * Select preview — the bare-`<select>` baseline: single mode gets the
 * `--k-chevron` custom arrow (the styled contract this wrapper guards), and
 * `multiple` mode gets its own padding/option rules.
 */
import * as React from "react";
import { Label, Select } from "@earnings-summary/design-system";

export function PeriodSingle() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>Period</Label>
      <Select defaultValue="Q2-2026">
        <option value="Q1-2026">Q1-2026</option>
        <option value="Q2-2026">Q2-2026</option>
        <option value="FY-2025">FY-2025</option>
      </Select>
    </div>
  );
}

export function TickerMultiple() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>Compare tickers</Label>
      <Select multiple size={5} defaultValue={["NU", "MELI", "RBRK"]}>
        <option value="NU">NU — Nu Holdings</option>
        <option value="MELI">MELI — MercadoLibre</option>
        <option value="NOW">NOW — ServiceNow</option>
        <option value="WIX">WIX — Wix.com</option>
        <option value="RBRK">RBRK — Rubrik</option>
      </Select>
    </div>
  );
}
