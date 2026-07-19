/**
 * Textarea preview — the bare-`<textarea>` form-control baseline from
 * controls.css. A thesis-note draft and an empty placeholder state.
 */
import * as React from "react";
import { Label, Textarea } from "@earnings-summary/design-system";

export function ThesisDraft() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 420 }}>
      <Label>Thesis note — RBRK</Label>
      <Textarea
        rows={4}
        defaultValue={
          "Subscription ARR grew 38% YoY with NRR above 120% — the land-and-expand motion is intact. " +
          "Watch cloud-module attach next quarter; if it stalls below 30% of new bookings, the platform re-rate case weakens."
        }
      />
    </div>
  );
}

export function EmptyPlaceholder() {
  return (
    <div style={{ maxWidth: 420 }}>
      <Textarea rows={3} placeholder="Add a note…" />
    </div>
  );
}
