/**
 * Pill preview — `.k-pill`, the ONE filled status/score badge: soft
 * color-mix status fill + token ink. Tone words come from the thesis-status
 * vocabulary (ok/warn/breach via thesisStatusTone), neutral base for
 * un-toned counts/scores.
 */
import * as React from "react";
import { Pill, TickerLabel } from "@earnings-summary/design-system";
import { thesisStatusTone } from "@earnings-summary/design-system";

export function ToneSweep() {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
      <Pill tone={thesisStatusTone("ok")}>OK</Pill>
      <Pill tone={thesisStatusTone("warn")}>WARN</Pill>
      <Pill tone={thesisStatusTone("breach")}>BREACH</Pill>
      <Pill tone="accent">PENDING</Pill>
    </div>
  );
}

export function Neutral() {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
      <Pill>12 facts</Pill>
      <Pill>Q2-2026</Pill>
      <Pill>unresolved</Pill>
    </div>
  );
}

export function ScoreRow() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <TickerLabel ticker="NU" name="Nu Holdings" />
        <Pill tone="ok">3.74</Pill>
        <Pill tone="ok">INTACT</Pill>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <TickerLabel ticker="WIX" name="Wix.com" />
        <Pill tone="warn">0.94</Pill>
        <Pill tone="warn">WATCH</Pill>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <TickerLabel ticker="RBRK" name="Rubrik" />
        <Pill tone="bad">-2.0pp</Pill>
        <Pill tone="bad">BREACH</Pill>
      </div>
    </div>
  );
}
