/**
 * Dot preview — `.k-dot`, the ONE filled circular status tick. Fill is
 * currentColor, tone only sets color; `size` sets `--k-dot-size`. Rendered
 * beside real freshness/pipeline labels so the card is never blank.
 */
import * as React from "react";
import { Dot } from "@earnings-summary/design-system";

export function StatusRows() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Dot tone="ok" /> <span>P1: 11 / 11 FRESH</span>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Dot tone="warn" /> <span>FMP cache: 2 STALE (WIX, RBRK)</span>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Dot tone="bad" /> <span>Morning pipeline: stage 0b failed</span>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Dot tone="muted" /> <span>Weekly eval rung: not yet run</span>
      </div>
    </div>
  );
}

export function Sized() {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
      <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
        <Dot tone="ok" /> <span>8px default</span>
      </span>
      <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
        <Dot tone="warn" size={12} /> <span>size 12</span>
      </span>
      <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
        <Dot tone="bad" size={16} /> <span>size 16</span>
      </span>
    </div>
  );
}
