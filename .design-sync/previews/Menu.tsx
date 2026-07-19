/**
 * Menu preview — `.k-menu`, the shared popover-list surface (combobox
 * results, palette rows). Declarative `items` mode with one `.sel` row, and
 * the raw-children escape hatch.
 */
import * as React from "react";
import { Menu, TickerLabel } from "@earnings-summary/design-system";

export function TickerResults() {
  return (
    <div style={{ maxWidth: 280 }}>
      <Menu
        items={[
          { key: "NU", label: <TickerLabel ticker="NU" name="Nu Holdings" /> },
          {
            key: "MELI",
            label: <TickerLabel ticker="MELI" name="MercadoLibre" />,
            selected: true,
          },
          { key: "NOW", label: <TickerLabel ticker="NOW" name="ServiceNow" /> },
          { key: "RBRK", label: <TickerLabel ticker="RBRK" name="Rubrik" /> },
        ]}
      />
    </div>
  );
}

export function PaletteRows() {
  return (
    <div style={{ maxWidth: 320 }}>
      <Menu>
        <li className="sel">Open workspace: WIX</li>
        <li>Re-run thesis evaluator</li>
        <li>Jump to Ledger: 4 need you</li>
        <li>Compare vs Q1-2026 KPIs</li>
      </Menu>
    </div>
  );
}
