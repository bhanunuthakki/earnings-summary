/**
 * TickerLabel preview — the canonical ticker + company-name label
 * (`ticker_label()` port): mono symbol, muted truncated name, full name on
 * hover. The replacement for every `f"{ticker} · {name}"` concatenation.
 */
import * as React from "react";
import { TickerLabel } from "@earnings-summary/design-system";

export function Basic() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <TickerLabel ticker="NU" name="Nu Holdings" />
      <TickerLabel ticker="MELI" name="MercadoLibre" />
      <TickerLabel ticker="NOW" name="ServiceNow" />
    </div>
  );
}

export function Linked() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <TickerLabel ticker="WIX" name="Wix.com" href="#wix" />
      <TickerLabel ticker="RBRK" name="Rubrik" href="#rbrk" />
    </div>
  );
}

export function Truncation() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <TickerLabel
        ticker="BKNG"
        name="Booking Holdings Incorporated (formerly The Priceline Group)"
        nameMax={18}
      />
      <TickerLabel ticker="GOOG" name="Alphabet Inc. Class C Capital Stock" nameMax="14ch" />
    </div>
  );
}

export function SymbolOnly() {
  return <TickerLabel ticker="META" />;
}
