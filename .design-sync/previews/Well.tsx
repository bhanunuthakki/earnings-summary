/**
 * Well preview — `.k-well`, the soft-filled BLOCK sibling of `.k-pill` (KPI
 * cards, callouts, tone rows): same color-mix tone family, box radius. Ink
 * stays `--fg`; status words inside take their own Pill.
 */
import * as React from "react";
import { Well, Pill, Label, NumText } from "@earnings-summary/design-system";

export function NeutralCallout() {
  return (
    <Well>
      NU Q2-2026 transcript landed 06:12 PT. Deposit growth re-accelerated to
      +49.1% YoY while NPL 15-90 held at 4.4% — no tier-1 break rule fired.
    </Well>
  );
}

export function ToneRow() {
  return (
    <div style={{ display: "flex", gap: 10 }}>
      <Well tone="ok">
        <Label>Revenue YoY</Label>
        <div>
          <NumText value={18.4} format={(n) => `+${n.toFixed(1)}%`} />
        </div>
      </Well>
      <Well tone="warn">
        <Label>Take rate</Label>
        <div>
          <NumText value={-0.4} format={(n) => `${n.toFixed(1)}pp`} />
        </div>
      </Well>
      <Well tone="bad">
        <Label>NRR</Label>
        <div>
          <NumText value={-2.0} format={(n) => `${n.toFixed(1)}pp`} />
        </div>
      </Well>
    </div>
  );
}

export function AccentWell() {
  return (
    <Well tone="accent">
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Pill tone="accent">PENDING</Pill>
        <span>3 answers waiting in the Ledger — walk the packet before 04:00.</span>
      </div>
    </Well>
  );
}
