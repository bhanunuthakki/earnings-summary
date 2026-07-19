/**
 * Chip preview — `.k-chip`, the ONE outline kind/filter tag (radius-full ·
 * micro · uppercase). Tones outline; `mono` for tickers/period codes;
 * `interactive` + `on` for the feed's filter row (accent reserved for the
 * selected chip, per design_language §2).
 */
import * as React from "react";
import { Chip } from "@earnings-summary/design-system";

export function ToneSweep() {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <Chip>Transcript</Chip>
      <Chip tone="ok">FRESH</Chip>
      <Chip tone="warn">STALE</Chip>
      <Chip tone="bad">Tier-1 break</Chip>
    </div>
  );
}

export function Mono() {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <Chip mono>Q2-2026</Chip>
      <Chip mono>MELI</Chip>
      <Chip mono tone="ok">FY-2025</Chip>
    </div>
  );
}

export function FilterRow() {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <Chip interactive on>
        All 14
      </Chip>
      <Chip interactive>Earnings 3</Chip>
      <Chip interactive>Thesis changes 11</Chip>
      <Chip interactive>News 6</Chip>
    </div>
  );
}
