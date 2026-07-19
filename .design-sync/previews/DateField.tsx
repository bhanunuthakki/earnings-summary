/**
 * DateField preview — the styled `<input type="date">` with the browser's
 * native calendar-picker indicator hidden and a kit calendar glyph in its
 * place (the native picker still opens on click). A Label-captioned field and
 * a bare one.
 */
import * as React from "react";
import { DateField, Label } from "@earnings-summary/design-system";

export function AsOfDate() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: 200 }}>
      <Label>As-of date</Label>
      <DateField defaultValue="2026-07-18" />
    </div>
  );
}

export function Bare() {
  return (
    <div style={{ maxWidth: 200 }}>
      <DateField defaultValue="2026-05-03" />
    </div>
  );
}
