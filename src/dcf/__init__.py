"""DCF subsystem.

Phase 3 implementation. The canonical DCF workbook lives at
`dcf/<TICKER>.xlsx` — user-maintained, hybrid round-trip:
  - User edits forecast assumptions, model formulas, segment cells in Excel.
  - System reads the FCF stream and diluted-shares cells from the Valuation
    sheet and re-computes PV/share at the per-ticker WACC (from
    `holdings/<TICKER>.json`).
  - PV / live-price comparison drives the trim/sell triggers per the design
    (>10% over → trim, >20% over → sell).

Phase 3e (this PR) adds automated Historicals lifecycle:
  - On a missing workbook, `seeder` copies an example template and writes
    20 quarters of standardized FMP financials into a new Historicals sheet.
  - On an existing workbook, `refresher` rewrites ONLY the Historicals sheet
    — Forecast / Model / Valuation cells round-trip byte-identical.

Modules:
  seeder          — copy template + populate Historicals (new workbook)
  refresher       — rewrite Historicals only (existing workbook)
  workbook_reader — extract FCF stream + diluted shares from an .xlsx
  valuation       — PV math (sum FCF_t/(1+wacc)^t + terminal * multiple/(1+wacc)^N)
  live_price      — pull current market price from FMP profile.json cache
  persist         — upsert dcf_runs row with all derived fields
"""
